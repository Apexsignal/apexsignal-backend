"""
ApexSignal — Tipsport Scraper
Modul: tipsport_scraper.py

Stahuje aktuální nabídku zápasů přímo z Tipsport REST API.
Tipsport používá vlastní interní REST API na:
  /rest/offer/v6/sports  — seznam všech soutěží s jejich ID
  /rest/offer/v2/offer   — zápasy a kurzy pro konkrétní soutěž

Volá se 1x denně (ráno) nebo při každém generování tiketu.
Vrátí seznam zápasů které Tipsport reálně nabízí — takže appka
generuje tikety POUZE z těchto zápasů.
"""

from __future__ import annotations
import requests
import time
from datetime import datetime, date
from typing import Optional


TIPSPORT_BASE = "https://www.tipsport.cz"

# Fotbalové soutěže které chceme sledovat — ID z Tipsport API
# (zjištěno z /rest/offer/v6/sports)
FOOTBALL_COMPETITION_IDS = [
    149535,  # MS 2026 - Kanada+Mexiko+USA - zápasy
    3152,    # Copa Libertadores
    9002,    # Evropský superpohár
    120,     # Česká Chance Liga
    118,     # 1. anglická liga
    39,      # 1. brazilská liga
    35488,   # 1. čínská liga
    24350,   # 1. estonská liga
    123,     # 1. finská liga
    33,      # 1. irská liga
    126,     # 1. islandská liga
    137652,  # 1. jihokorejská liga
    69075,   # 1. kazašská liga
    131,     # 1. norská liga
    144,     # 1. švédská liga
    50668,   # 2. argentinská liga
    50188,   # 2. brazilská liga
    87227,   # USA - USL Championship
    50,      # 2. norská liga
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "cs-CZ,cs;q=0.9",
    "Referer": "https://www.tipsport.cz/kurzy/fotbal-16",
    "Origin": "https://www.tipsport.cz",
}


def get_active_competitions() -> list[dict]:
    """
    Stáhne aktuální seznam aktivních fotbalových soutěží z Tipsportu.
    Vrátí seznam {id, title, count} — jen soutěže které mají aspoň 1 zápas.
    """
    url = f"{TIPSPORT_BASE}/rest/offer/v6/sports"
    params = {
        "fromResults": "false",
        "withLive": "true",
        "mySelectionWithLiveMatches": "true",
    }
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[tipsport_scraper] Chyba při načítání soutěží: {e}")
        return []

    competitions = []
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "COMPETITION" and node.get("count", 0) > 0:
                competitions.append({
                    "id": node["id"],
                    "title": node.get("title", ""),
                    "count": node.get("count", 0),
                    "url": node.get("url", ""),
                })
            for child in node.get("children", []):
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data.get("data", {}))
    return competitions


def get_matches_for_competition(competition_id: int) -> list[dict]:
    """
    Stáhne zápasy s kurzy pro konkrétní soutěž.
    """
    url = f"{TIPSPORT_BASE}/rest/offer/v2/offer"
    params = {
        "limit": 75,
        "offset": 0,
        "categoryId": competition_id,
    }
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[tipsport_scraper] Chyba při načítání zápasů pro {competition_id}: {e}")
        return []

    matches = []

    # Tipsport vrací data v offerSuperSports -> různé úrovně
    def extract_matches(node):
        if isinstance(node, list):
            for item in node:
                extract_matches(item)
        elif isinstance(node, dict):
            # Pokud je to zápas (má participants)
            if "participants" in node and len(node.get("participants", [])) >= 2:
                parsed = _parse_match(node)
                if parsed:
                    matches.append(parsed)
            # Rekurzivně prohledej všechny klíče
            for key, val in node.items():
                if isinstance(val, (dict, list)):
                    extract_matches(val)

    extract_matches(data)

    # Odstraň duplikáty podle tipsport_id
    seen = set()
    unique = []
    for m in matches:
        mid = m.get("tipsport_id")
        if mid not in seen:
            seen.add(mid)
            unique.append(m)

    return unique


def _parse_match(match: dict) -> Optional[dict]:
    """Normalizuje raw Tipsport match na interní formát."""
    try:
        participants = match.get("participants", [])
        if len(participants) < 2:
            return None

        home = participants[0].get("name", "")
        away = participants[1].get("name", "")

        # Čas zápasu
        start_time_ms = match.get("startTime")
        if start_time_ms:
            start_dt = datetime.fromtimestamp(start_time_ms / 1000)
            match_date = start_dt.date()
            match_time = start_dt.strftime("%H:%M")
        else:
            match_date = date.today()
            match_time = "??:??"

        # Kurzy — hledáme 1X2 a Over/Under gólů
        odds = _extract_odds(match)

        return {
            "tipsport_id": match.get("id"),
            "home_team": home,
            "away_team": away,
            "date": str(match_date),
            "time": match_time,
            "competition_id": match.get("competitionId"),
            "competition_name": match.get("competitionName", ""),
            "odds_1x2": odds.get("1x2"),        # {"home": 1.32, "draw": 5.49, "away": 3.65}
            "odds_over_15": odds.get("over_1.5"),
            "odds_over_25": odds.get("over_2.5"),
            "odds_btts_yes": odds.get("btts_yes"),
        }
    except Exception as e:
        print(f"[tipsport_scraper] Chyba při parsování zápasu: {e}")
        return None


def _extract_odds(match: dict) -> dict:
    """Vytáhne kurzy z Tipsport match struktury."""
    result = {}
    for odds_group in match.get("odds", []):
        name = odds_group.get("name", "").lower()
        values = odds_group.get("odds", [])

        # 1X2 - Výsledek zápasu
        if "výsledek" in name or "vítěz" in name or "1x2" in name:
            if len(values) >= 3:
                result["1x2"] = {
                    "home": _safe_odds(values[0]),
                    "draw": _safe_odds(values[1]),
                    "away": _safe_odds(values[2]),
                }
        # Over/Under gólů
        elif "počet gólů" in name or "over" in name or "gól" in name:
            for val in values:
                label = val.get("name", "").lower()
                odds_val = _safe_odds(val)
                if "více než 1.5" in label or "over 1.5" in label:
                    result["over_1.5"] = odds_val
                elif "více než 2.5" in label or "over 2.5" in label:
                    result["over_2.5"] = odds_val
        # BTTS
        elif "oba" in name or "btts" in name:
            for val in values:
                label = val.get("name", "").lower()
                if "ano" in label or "yes" in label:
                    result["btts_yes"] = _safe_odds(val)

    return result


def _safe_odds(val) -> Optional[float]:
    """Bezpečně vytáhne kurz z různých formátů."""
    if isinstance(val, (int, float)):
        return round(float(val), 2)
    if isinstance(val, dict):
        raw = val.get("value") or val.get("odds") or val.get("rate")
        if raw:
            try:
                return round(float(raw), 2)
            except (ValueError, TypeError):
                return None
    return None


def get_todays_football_matches() -> list[dict]:
    """
    Hlavní funkce — vrátí všechny dnešní fotbalové zápasy z Tipsportu
    z předem definovaných soutěží. Výsledek lze použít přímo
    jako filtr pro ApexSignal generátor tiketů.
    """
    all_matches = []
    today = str(date.today())

    print(f"[tipsport_scraper] Stahuji zápasy pro {len(FOOTBALL_COMPETITION_IDS)} soutěží...")

    for comp_id in FOOTBALL_COMPETITION_IDS:
        matches = get_matches_for_competition(comp_id)
        today_matches = [m for m in matches if m.get("date") == today]
        if today_matches:
            print(f"[tipsport_scraper] Soutěž {comp_id}: {len(today_matches)} dnešních zápasů")
            all_matches.extend(today_matches)
        time.sleep(0.3)  # pauza mezi requesty

    print(f"[tipsport_scraper] Celkem {len(all_matches)} dnešních zápasů z Tipsportu")
    return all_matches


def get_upcoming_football_matches(days: int = 3) -> list[dict]:
    """
    Vrátí zápasy na příštích N dní ze všech sledovaných soutěží.
    """
    all_matches = []
    today = date.today()

    for comp_id in FOOTBALL_COMPETITION_IDS:
        matches = get_matches_for_competition(comp_id)
        for m in matches:
            try:
                match_date = date.fromisoformat(m.get("date", ""))
                days_diff = (match_date - today).days
                if 0 <= days_diff <= days:
                    all_matches.append(m)
            except ValueError:
                continue
        time.sleep(0.3)

    print(f"[tipsport_scraper] {len(all_matches)} zápasů na příštích {days} dní")
    return all_matches


# ── Test ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== Test: aktivní soutěže na Tipsportu ===")
    comps = get_active_competitions()
    football = [c for c in comps if "fotbal" in c.get("url", "").lower()]
    print(f"Fotbalové soutěže s aktivní nabídkou: {len(football)}")
    for c in football[:10]:
        print(f"  ID={c['id']} ({c['count']} příl.) — {c['title']}")

    print("\n=== Test: dnešní zápasy MS 2026 ===")
    matches = get_matches_for_competition(149535)
    print(f"Nalezeno {len(matches)} zápasů")
    for m in matches[:5]:
        print(f"  {m['home_team']} vs {m['away_team']} | {m['date']} {m['time']}")
        print(f"    1X2: {m.get('odds_1x2')}")
        print(f"    Over 2.5: {m.get('odds_over_25')}")
