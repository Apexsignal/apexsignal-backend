"""
ApexSignal — AI kontrola výběrů
Modul: ai_reviewer.py

Dvě funkce:
1. review_candidates() — Claude kontroluje všechny kandidáty BEZ web searche
   (rychlé, 3-5s), na základě znalostí o týmech, ligách a kontextu.
2. review_live_signal() — Claude kontroluje živý signál S web searchem
   (krátký timeout 10s), hledá čerstvé zprávy o konkrétním zápase.
"""

from __future__ import annotations

import json
import os

import requests

from probability_model import SelectionCandidate

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"
REVIEW_TIMEOUT_SECONDS = 30
LIVE_SIGNAL_TIMEOUT_SECONDS = 10


def review_candidates(candidates: list[SelectionCandidate]) -> list[SelectionCandidate]:
    """
    Projde VŠECHNY kandidáty najednou — Claude kontroluje bez web searche
    (rychlé, spolehlivé). Hledá zjevné problémy: neznámé týmy, nerealistické
    pravděpodobnosti, podezřelé kombinace. Při jakékoli chybě vrátí původní seznam.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not candidates:
        return candidates

    listing = "\n".join(
        f"{i}. {c.home_team} vs {c.away_team} — "
        f"trh: {c.market_type.value}, výběr: {c.selection}, "
        f"pravděpodobnost: {round(c.probability * 100)} %, "
        f"kurz: {c.odds}"
        for i, c in enumerate(candidates)
    )

    prompt = (
        "Jsi expert na sportovní sázení. Níže je seznam sázkových výběrů "
        "které statistický model označil jako potenciálně zajímavé "
        "(pravděpodobnost >70 %). \n\n"
        f"{listing}\n\n"
        "Pro každý výběr zhodnoť jestli je rozumný — na základě svých znalostí "
        "o týmech, ligách a typických výsledcích. Zamítni výběr pokud:\n"
        "- pravděpodobnost vypadá nerealisticky vysoká pro daný zápas\n"
        "- kurz neodpovídá pravděpodobnosti (podezřelá nekonzistence)\n"
        "- jde o velmi nevyrovnaný zápas kde model mohl přestřelit\n\n"
        "Pokud o týmech nic nevíš, výběr potvrď (keep: true).\n\n"
        "Odpověz POUZE JSON polem, žádný další text ani markdown, kde "
        "každý prvek má tvar: "
        '{"index": <číslo>, "keep": true/false, "note": "<max 8 slov česky>"}.'
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
                # BEZ web_search tool — rychlé a spolehlivé
            },
            timeout=REVIEW_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()

        text = "".join(
            block.get("text", "") for block in data.get("content", [])
            if block.get("type") == "text"
        ).strip()

        # Vyčisti případné markdown backticky
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:].strip()

        verdicts = json.loads(text)
        keep_map = {v["index"]: v.get("keep", True) for v in verdicts if "index" in v}

        # Loguj rozhodnutí Claudea
        for i, c in enumerate(candidates):
            keep = keep_map.get(i, True)
            note = next((v.get("note", "") for v in verdicts if v.get("index") == i), "")
            status = "✓" if keep else "✗ ZAMÍTNUT"
            print(f"[ai_reviewer] {status} {c.home_team} vs {c.away_team} {c.market_type.value}: {note}")

        reviewed = [c for i, c in enumerate(candidates) if keep_map.get(i, True)]

        # Pokud by Claude zamítl úplně vše, vracíme původní pool
        if not reviewed:
            print("[ai_reviewer] Claude zamítl vše — ignoruji, vracím původní pool")
            return candidates

        print(f"[ai_reviewer] Claude potvrdil {len(reviewed)}/{len(candidates)} kandidátů")
        return reviewed

    except Exception as exc:
        print(f"[ai_reviewer] AI kontrola selhala, pokračuji bez ní: {exc}")
        return candidates


def review_live_signal(signal, home_team: str, away_team: str) -> str | None:
    """
    Pro živý signál — Claude S web searchem hledá čerstvé zprávy
    o konkrétním zápase. Krátký timeout, nikdy nezablokuje signál.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    side_label = "domácích" if signal.team_side == "home" else "hostů"
    prompt = (
        f"Právě běží zápas {home_team} vs {away_team}. "
        f"Statistický model vyhodnotil silný herní tlak týmu {side_label}. "
        f"Vyhledej jestli existuje ČERSTVÁ zpráva o tomto zápase která by "
        f"tlak zpochybnila (zranění, vyloučení, něco neobvyklého). "
        f"Pokud nic nenajdeš, napiš přesně 'bez nálezu'. Max 2 věty česky."
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
            block.get("text", "") for block in data.get("content", [])
            if block.get("type") == "text"
        ).strip()
        if not text or "bez nálezu" in text.lower():
            return None
        return text

    except Exception as exc:
        print(f"[ai_reviewer] Kontrola živého signálu selhala: {exc}")
        return None
