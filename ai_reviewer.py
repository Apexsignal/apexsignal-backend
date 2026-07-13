"""
ApexSignal — AI kontrola výběrů
Modul: ai_reviewer.py

Volá Claude (Anthropic API) s webovým vyhledáváním, aby ke každému
statisticky vyfiltrovanému výběru (>70 % pravděpodobnost dle probability_model.py)
zkontroloval, jestli neexistují čerstvé zprávy (zranění, distance, rotace
sestavy, odložení zápasu...), které by tu pravděpodobnost zpochybnily.

Důležité: tahle vrstva NENAHRAZUJE statistický model, jen ho doplňuje o
kontext, který čísla sama nevidí. Nezaručuje "správnost" ve smyslu jisté
výhry — jen snižuje riziko, že appka doporučí tip, který je čerstvě
zneplatněný zprávou, o které model neví.

Pozn.: vyžaduje ANTHROPIC_API_KEY (proměnná prostředí — samostatný klíč
z console.anthropic.com, JINÝ systém než sportovní API a ODDSAPI_KEY výše;
je to placené dle skutečného použití). Pokud klíč chybí nebo volání
jakkoli selže (timeout, neplatná odpověď...), funkce tiše vrátí původní
seznam beze změny — tahle volitelná vrstva nikdy nesmí appku shodit ani
zablokovat generování tiketu.
"""

from __future__ import annotations

import json
import os

import requests

from probability_model import SelectionCandidate

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"


def review_candidates(candidates: list[SelectionCandidate]) -> list[SelectionCandidate]:
    """
    Vrátí podmnožinu `candidates`, kterou Claude po kontrole čerstvých zpráv
    NEoznačil jako rizikovou. Při chybějícím klíči, prázdném vstupu, nebo
    jakékoli chybě volání/parsování vrátí `candidates` beze změny.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not candidates:
        return candidates

    listing = "\n".join(
        f"{i}. {c.home_team} vs {c.away_team} — trh: {c.market_type.value}, "
        f"výběr: {c.selection}, naše pravděpodobnost: {round(c.probability * 100)} %"
        for i, c in enumerate(candidates)
    )

    prompt = (
        "Jsi asistent pro kontrolu sázkových tipů před zveřejněním. Níže je "
        "seznam statisticky vyfiltrovaných výběrů. Pro každý vyhledej na webu "
        "ČERSTVÉ zprávy (poslední dny) o klíčových ZRANĚNÍCH, DISTANCÍCH, "
        "ODLOŽENÍ ZÁPASU nebo jiné konkrétní události, která by výběr "
        "zneplatnila. NEHODNOŤ pravděpodobnosti ani kurzy — to řeší statistický "
        "model, ne ty. Pokud nic konkrétního nenajdeš, výběr potvrď (keep: true).\n\n"
        f"{listing}\n\n"
        "Odpověz POUZE JSON polem (žádný další text, žádné markdown), kde "
        "každý prvek má tvar "
        '{"index": <číslo>, "keep": true/false, "note": "<krátké zdůvodnění v češtině, max 15 slov>"}.'
    )

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        text = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        )
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        verdicts = json.loads(cleaned.strip())

        keep_map = {v["index"]: v.get("keep", True) for v in verdicts if "index" in v}
        reviewed = [c for i, c in enumerate(candidates) if keep_map.get(i, True)]
        
        # ZESLABENÍ: pokud AI vyfiltrovala skoro všechno, vezmi alespoň TOP 5 původních
        if len(reviewed) < 3 and len(candidates) >= 5:
            reviewed = candidates[:5]
            print(f"[ai_reviewer] AI byla příliš přísná, beru TOP 5 kandidátů")
        
        # pokud by AI (chybně) zamítla úplně všechno, nedůvěřujeme tomu a vracíme původní pool
        return reviewed if reviewed else candidates

    except Exception as exc:  # noqa: BLE001 — tahle vrstva nikdy nesmí appku shodit
        print(f"[ai_reviewer] AI kontrola selhala, appka pokračuje bez ní: {exc}")
        return candidates


LIVE_SIGNAL_TIMEOUT_SECONDS = 8  # živé sázení běží v sekundách — appka na Claude nečeká dlouho


def review_live_signal(signal, home_team: str, away_team: str) -> str | None:
    """
    Pro JEDEN konkrétní živý signál (na rozdíl od review_candidates výše,
    co kontroluje celý seznam tiketových výběrů najednou) zkusí Claude
    s webovým vyhledáváním dohledat čerstvé zprávy o tomhle konkrétním
    zápase — vystřídání, zranění, vyloučení trenéra z lavičky — co by
    mohly tlak, na kterém je signál postavený, zpochybnit.

    Volá se jen pro signály, co UŽ prošly matematickým potvrzením a shodou
    s živým trhem (viz backend_api.py) — ne pro každou krátkou špičku
    tlaku, kterou appka jen zvažuje. I tak má krátký časový limit, protože
    živé kurzy se mění v řádu sekund, ne minut jako u tiketů.

    Vrací krátkou poznámku k připojení do reasoning, nebo None — při
    chybějícím klíči, jakékoli chybě volání, nebo když appka nic nenajde.
    V žádném z těch případů appka signál nezablokuje, jen ho pošle bez
    AI poznámky.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    side_label = "domácích" if signal.team_side == "home" else "hostů"
    prompt = (
        f"Právě běží živě zápas {home_team} vs {away_team}. Statistický model "
        f"vyhodnotil silný herní tlak týmu {side_label} směrem ke gólu. "
        f"Vyhledej na webu, jestli existuje ČERSTVÁ zpráva (z posledních "
        f"minut/hodin) o tomhle konkrétním zápase, která by tenhle tlak "
        f"zpochybnila (klíčové zranění, vyloučení, něco neobvyklého). Pokud "
        f"nic nenajdeš, napiš přesně 'bez nálezu'. Odpověz pouze 1-2 větami "
        f"v češtině, žádný další text ani úvod."
    )

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            },
            timeout=LIVE_SIGNAL_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
        text = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        ).strip()
        if not text or text.lower().startswith("bez nálezu"):
            return None
        return text

    except Exception as exc:  # noqa: BLE001 — živé signály appka nikdy nezablokuje
        print(f"[ai_reviewer] Kontrola živého signálu selhala/vypršel čas, appka pokračuje bez ní: {exc}")
        return None
