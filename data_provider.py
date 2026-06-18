"""
ApexSignal — Integrační vrstva pro sportovní data a kurzy
Modul: data_provider.py

Účel:
    Abstrahuje konkrétního API providera (např. API-Football, Sportradar,
    Betfair Exchange, Pinnacle API...) za jednotné rozhraní, které
    `probability_model.py` a `momentum_filter.py` konzumují bez znalosti
    konkrétního externího kontraktu.

    Obsahuje:
      - SportsDataProvider: abstraktní rozhraní
      - HttpSportsDataProvider: referenční implementace přes obecné REST API
      - InMemoryCache: jednoduchý TTL cache layer (omezuje počet API callů)
      - normalizační funkce -> MatchInput (pro generátor tiketů)
                              -> MatchSnapshot (pro Momentum Filter)

    Pozn.: Reálné API klíče se dosazují přes proměnné prostředí (APISPORTS_KEY,
    APITENNIS_KEY, ODDSAPI_KEY) — nastav je na serveru, kde poběží backend —
    nikdy ne v kódu ani ve frontend souborech.
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from probability_model import MatchInput, Sport, MarketType, devig_market, devig_two_way
from momentum_filter import MatchSnapshot


# ---------------------------------------------------------------------
# Jednoduchý TTL cache (snižuje zátěž na rate-limited API)
# ---------------------------------------------------------------------
class InMemoryCache:
    def __init__(self, ttl_seconds: int = 30):
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str):
        item = self._store.get(key)
        if not item:
            return None
        expires_at, value = item
        if time.time() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value) -> None:
        self._store[key] = (time.time() + self._ttl, value)


# ---------------------------------------------------------------------
# Abstraktní rozhraní — implementuje jakýkoli konkrétní data provider
# ---------------------------------------------------------------------
class SportsDataProvider(ABC):
    """Společné rozhraní pro pre-match statistiky i live data."""

    @abstractmethod
    def get_upcoming_matches(self, sport: Sport, days_ahead: int) -> list[dict]:
        """Vrátí raw seznam zápasů v daném časovém okně."""
        raise NotImplementedError

    @abstractmethod
    def get_team_statistics(self, sport: Sport, team_id: str) -> dict:
        """Vrátí historická data pro výpočet expected_goals/expected_cards (xG model)."""
        raise NotImplementedError

    @abstractmethod
    def get_pre_match_odds(self, match_id: str) -> dict:
        """Vrátí aktuální kurzy pro hlavní trhy (1X2, over/under gólů, karet)."""
        raise NotImplementedError

    @abstractmethod
    def get_live_match_stats(self, match_id: str) -> dict:
        """Vrátí aktuální minutu-po-minutě statistiku běžícího zápasu."""
        raise NotImplementedError


# ---------------------------------------------------------------------
# Referenční HTTP implementace (obecná, použitelná pro většinu REST API
# typu API-Football / Sportmonks / Sportradar po doplnění mapování polí)
# ---------------------------------------------------------------------
class HttpSportsDataProvider(SportsDataProvider):
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 cache_ttl_seconds: int = 30):
        self.base_url = base_url or os.environ.get("SPORTS_API_BASE_URL", "")
        self.api_key = api_key or os.environ.get("SPORTS_API_KEY", "")
        self._cache = InMemoryCache(ttl_seconds=cache_ttl_seconds)

    # -- HTTP helper -----------------------------------------------------
    def _request(self, path: str, params: Optional[dict] = None) -> dict:
        """
        Skutečnou implementaci doplň dle vybraného providera, např.:

            import requests
            resp = requests.get(
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self.api_key}"},
                params=params, timeout=5,
            )
            resp.raise_for_status()
            return resp.json()

        Zde necháváme stub, aby byl modul testovatelný bez síťového přístupu.
        """
        raise NotImplementedError(
            "Doplň HTTP klienta pro konkrétního providera (viz docstring metody)."
        )

    def get_upcoming_matches(self, sport: Sport, days_ahead: int) -> list[dict]:
        cache_key = f"upcoming:{sport.value}:{days_ahead}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        data = self._request("/fixtures", {"sport": sport.value, "days": days_ahead})
        self._cache.set(cache_key, data)
        return data

    def get_team_statistics(self, sport: Sport, team_id: str) -> dict:
        cache_key = f"team_stats:{sport.value}:{team_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        data = self._request(f"/teams/{team_id}/statistics", {"sport": sport.value})
        self._cache.set(cache_key, data)
        return data

    def get_pre_match_odds(self, match_id: str) -> dict:
        # kurzy se nekešují (nebo jen velmi krátce) — měly by být co nejčerstvější
        return self._request(f"/odds/{match_id}")

    def get_live_match_stats(self, match_id: str) -> dict:
        # live data se nekešují vůbec — vždy aktuální stav
        return self._request(f"/live/{match_id}")


# ---------------------------------------------------------------------
# Normalizace: raw API response -> interní datové struktury
# ---------------------------------------------------------------------
def normalize_to_match_input(
    sport: Sport,
    fixture: dict,
    home_stats: dict,
    away_stats: dict,
    odds_raw: dict,
) -> MatchInput:
    """
    Převede syrová data z providera na MatchInput konzumovaný
    probability_model.TicketGenerator. Mapování klíčů (`fixture["..."]`)
    je třeba upravit dle konkrétního API kontraktu.
    """
    home_xg = _estimate_expected_goals(home_stats, is_home=True)
    away_xg = _estimate_expected_goals(away_stats, is_home=False)
    expected_cards = _estimate_expected_cards(home_stats, away_stats)

    return MatchInput(
        match_id=fixture["id"],
        sport=sport,
        home_team=fixture["home_team"],
        away_team=fixture["away_team"],
        home_expected_goals=home_xg,
        away_expected_goals=away_xg,
        expected_cards=expected_cards,
        favorite_win_market_odds=odds_raw.get("match_winner", {}).get("favorite", 1.0),
        over_goals_odds=odds_raw.get("over_goals", {}),     # {2.5: 1.85, 3.5: 2.60, ...}
        over_cards_odds=odds_raw.get("over_cards", {}),     # {3.5: 1.90, 4.5: 2.40, ...}
    )


def normalize_to_match_snapshot(match_id: int, live_raw: dict) -> MatchSnapshot:
    """Převede raw live data na MatchSnapshot konzumovaný MomentumFilter."""
    return MatchSnapshot(
        minute=live_raw["minute"],
        home_possession=live_raw["possession"]["home"],
        away_possession=live_raw["possession"]["away"],
        home_shots_on_target=live_raw["shots_on_target"]["home"],
        away_shots_on_target=live_raw["shots_on_target"]["away"],
        home_dangerous_attacks=live_raw["dangerous_attacks"]["home"],
        away_dangerous_attacks=live_raw["dangerous_attacks"]["away"],
        home_corners=live_raw.get("corners", {}).get("home", 0),
        away_corners=live_raw.get("corners", {}).get("away", 0),
        red_cards_home=live_raw.get("red_cards", {}).get("home", 0),
        red_cards_away=live_raw.get("red_cards", {}).get("away", 0),
    )


def _estimate_expected_goals(team_stats: dict, is_home: bool) -> float:
    """
    Zjednodušený xG odhad: průměr vstřelených gólů za posledních N zápasů,
    upravený o domácí/venkovní výhodu. V produkci nahraď plnohodnotným
    xG modelem (Dixon-Coles, Poisson regrese s útočnou/obrannou silou týmu).
    """
    avg_goals_scored = team_stats.get("avg_goals_scored_last_10", 1.2)
    home_advantage_factor = 1.10 if is_home else 0.92
    return round(avg_goals_scored * home_advantage_factor, 2)


def _estimate_expected_cards(home_stats: dict, away_stats: dict) -> float:
    home_avg = home_stats.get("avg_cards_last_10", 2.0)
    away_avg = away_stats.get("avg_cards_last_10", 2.0)
    return round(home_avg + away_avg, 2)


# ---------------------------------------------------------------------
# Factory — vybere providera dle sportu (různé sporty mívají různé API)
# ---------------------------------------------------------------------
def _current_season_string(hyphenated: bool = True) -> str:
    """Basketball/Hockey sezóny jsou typicky '2025-2026' (přes přelom roku), fotbal jen rokem."""
    today = date.today()
    if not hyphenated:
        return str(today.year)
    if today.month >= 8:
        return f"{today.year}-{today.year + 1}"
    return f"{today.year - 1}-{today.year}"


# =======================================================================
# BASKETBALL + HOCKEY — přímo přes dashboard.api-sports.io (NE RapidAPI)
# Jeden klíč (APISPORTS_KEY) pokrývá oba sporty zdarma (100 req/den).
# Dokumentace: api-sports.io/documentation/basketball/v1 a /hockey/v1
# =======================================================================
class APISportsDirectProvider(SportsDataProvider):
    def __init__(self, sport_path: str, api_key: Optional[str] = None, cache_ttl_seconds: int = 30):
        self.sport_path = sport_path  # "basketball" nebo "hockey"
        self.api_key = api_key or os.environ.get("APISPORTS_KEY", "")
        if not self.api_key:
            raise RuntimeError(
                "Chybí APISPORTS_KEY. Tohle je klíč z dashboard.api-sports.io "
                "(přímá registrace, NE RapidAPI — jiný klíč, jiná autentizace)."
            )
        self.base_url = f"https://v1.{sport_path}.api-sports.io"
        self._cache = InMemoryCache(ttl_seconds=cache_ttl_seconds)

    def _get(self, path: str, params: dict) -> list:
        resp = requests.get(
            f"{self.base_url}{path}", headers={"x-apisports-key": self.api_key},
            params=params, timeout=8,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"API-{self.sport_path.capitalize()} vrátilo chybu: {payload['errors']}")
        return payload.get("response", [])

    def get_upcoming_matches(self, sport: Sport, days_ahead: int) -> list[dict]:
        cache_key = f"upcoming:{days_ahead}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        games: list[dict] = []
        today = date.today()
        for offset in range(days_ahead + 1):
            day = today + timedelta(days=offset)
            games.extend(self._get("/games", {"date": day.isoformat()}))
        self._cache.set(cache_key, games)
        return games

    def get_team_statistics(self, sport: Sport, team_id: str) -> dict:
        cache_key = f"team_stats:{team_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        # Pozn.: v praxi tahle endpoint často vyžaduje i 'league' parametr —
        # doplň ID ligy, kterou sleduješ (obdoba WATCHED_LEAGUE_IDS u fotbalu).
        season = _current_season_string(hyphenated=True)
        response = self._get("/teams/statistics", {"team": team_id, "season": season})
        data = response if isinstance(response, dict) else (response[0] if response else {})
        self._cache.set(cache_key, data)
        return data

    def get_pre_match_odds(self, match_id: str) -> dict:
        response = self._get("/odds", {"game": match_id})
        return response[0] if response else {}

    def get_live_match_stats(self, match_id: str) -> dict:
        stats = self._get("/games/statistics/teams", {"id": match_id})
        return {"statistics": stats}


def adapt_apisports_game(game: dict) -> dict:
    """
    Společný adaptér pro Basketball/Hockey '/games' (api-sports.io).
    Pozn.: dokumentace bohužel nemá plný JSON příklad pro /games — tyto cesty
    klíčů (id/teams.home.id/teams.home.name) vycházejí z konvence, kterou
    API-Sports používá ve fotbalu i NBA API. Ověř si to při prvním reálném
    callu a uprav, pokud se nějaký název liší.
    """
    return {
        "id": game.get("id"),
        "home_team": game["teams"]["home"]["name"],
        "away_team": game["teams"]["away"]["name"],
        "home_team_id": game["teams"]["home"]["id"],
        "away_team_id": game["teams"]["away"]["id"],
    }


def adapt_apisports_basketball_team_stats(stats: dict) -> dict:
    """
    '/teams/statistics' (basketball) → průměr bodů a trojek ZA TENTO TÝM
    (sečti home+away v backend_api.py pro odhad celkového skóre zápasu).
    Pozn.: ověř přesnou cestu klíčů proti reálné odpovědi.
    """
    points_avg = stats.get("points", {}).get("for", {}).get("average", {}).get("all", "105.0")
    threes_avg = stats.get("threepoint_goals", {}).get("for", {}).get("average", {}).get("all", "12.0")
    return {
        "points_avg": float(points_avg or 105.0),
        "threes_avg": float(threes_avg or 12.0),
    }


def adapt_apisports_hockey_team_stats(stats: dict) -> dict:
    """
    '/teams/statistics' (hokej) → průměr gólů za tento tým.
    Pozn.: API-Hockey pravděpodobně nemá trestné minuty jako přímou
    agregovanou statistiku (podobně jako fotbal nemá 'dangerous attacks') —
    expected_penalty_minutes je tu konzervativní placeholder (6.0 na tým),
    uprav, jakmile zjistíš skutečnou strukturu odpovědi.
    """
    goals_avg = stats.get("goals", {}).get("for", {}).get("average", {}).get("all", "3.0")
    return {
        "goals_avg": float(goals_avg or 3.0),
        "penalty_minutes_avg": 6.0,
    }


# =======================================================================
# TENIS — api-tennis.com (autentizace přes query parametr APIkey, NE header!)
# Dokumentace: https://api-tennis.com/documentation
# =======================================================================
class APITennisProvider(SportsDataProvider):
    BASE_URL = "https://api.api-tennis.com/tennis/"

    def __init__(self, api_key: Optional[str] = None, cache_ttl_seconds: int = 30):
        self.api_key = api_key or os.environ.get("APITENNIS_KEY", "")
        if not self.api_key:
            raise RuntimeError("Chybí APITENNIS_KEY (proměnná prostředí).")
        self._cache = InMemoryCache(ttl_seconds=cache_ttl_seconds)

    def _get(self, method: str, params: dict) -> list:
        query = {"method": method, "APIkey": self.api_key, **params}
        resp = requests.get(self.BASE_URL, params=query, timeout=8)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("success") != 1:
            raise RuntimeError(f"api-tennis.com vrátilo chybu: {payload}")
        return payload.get("result", [])

    def get_upcoming_matches(self, sport: Sport, days_ahead: int) -> list[dict]:
        cache_key = f"upcoming:{days_ahead}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        today = date.today()
        fixtures = self._get("get_fixtures", {
            "date_start": today.isoformat(),
            "date_stop": (today + timedelta(days=days_ahead)).isoformat(),
        })
        self._cache.set(cache_key, fixtures)
        return fixtures

    def get_team_statistics(self, sport: Sport, team_id: str) -> dict:
        """U tenisu jde reálně o hráče, ne tým — parametr team_id = player_key."""
        cache_key = f"player_stats:{team_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        response = self._get("get_players", {"player_key": team_id})
        data = response[0] if response else {}
        self._cache.set(cache_key, data)
        return data

    def get_pre_match_odds(self, match_id: str) -> dict:
        response = self._get("get_odds", {"match_key": match_id})
        return response[0] if response else {}

    def get_live_match_stats(self, match_id: str) -> dict:
        response = self._get("get_livescore", {"match_key": match_id})
        return response[0] if response else {}


def adapt_api_tennis_fixture(fixture: dict) -> dict:
    return {
        "id": fixture["event_key"],
        "home_team": fixture["event_first_player"],
        "away_team": fixture["event_second_player"],
        "home_team_id": fixture["first_player_key"],
        "away_team_id": fixture["second_player_key"],
    }


def adapt_api_tennis_player_stats(player: dict) -> dict:
    """
    api-tennis.com nemá v dokumentaci přímo agregovaná esa/gamy na zápas —
    vrací jen win/loss rekord. expected_total_games a expected_total_aces
    jsou proto konzervativní pevné odhady (typický počet pro daný formát),
    NE odvozené z reálných dat hráče. win_rate aspoň reálně vychází
    z matches_won/matches_lost. Pro přesnější odhad gamů/es by bylo potřeba
    parsovat historii skóre (event_final_result) z get_fixtures — to tu
    není implementováno.
    """
    stats_list = player.get("stats", [])
    current = stats_list[0] if stats_list else {}
    won = int(current.get("matches_won") or 0)
    lost = int(current.get("matches_lost") or 0)
    win_rate = won / (won + lost) if (won + lost) > 0 else 0.5
    return {"win_rate": win_rate}


# =======================================================================
# ŽIVÉ KURZY — the-odds-api.com (samostatná vrstva, kombinuje se s výše
# uvedenými providery). Dokumentace: the-odds-api.com/liveapi/guides/v4
# =======================================================================
class OddsAPIProvider:
    BASE_URL = "https://api.the-odds-api.com/v4"

    # the-odds-api nemá jeden obecný klíč pro "fotbal" — každá soutěž má
    # vlastní sport_key. Doplň/uprav podle lig, které chceš sledovat.
    SPORT_KEYS: dict[Sport, list[str]] = {
        Sport.FOOTBALL: ["soccer_epl", "soccer_uefa_champs_league"],
        Sport.BASKETBALL: ["basketball_nba"],
        Sport.HOCKEY: ["icehockey_nhl"],
        Sport.TENNIS: [],  # turnajové klíče se mění (např. "tennis_atp_french_open") — doplň aktuální
    }

    def __init__(self, api_key: Optional[str] = None, cache_ttl_seconds: int = 30):
        self.api_key = api_key or os.environ.get("ODDSAPI_KEY", "")
        if not self.api_key:
            raise RuntimeError("Chybí ODDSAPI_KEY (proměnná prostředí).")
        self._cache = InMemoryCache(ttl_seconds=cache_ttl_seconds)

    def get_odds(self, sport: Sport, markets: str = "h2h,totals", regions: str = "eu") -> list[dict]:
        events: list[dict] = []
        for sport_key in self.SPORT_KEYS.get(sport, []):
            cache_key = f"odds:{sport_key}:{markets}"
            cached = self._cache.get(cache_key)
            if cached is not None:
                events.extend(cached)
                continue
            resp = requests.get(
                f"{self.BASE_URL}/sports/{sport_key}/odds",
                params={"apiKey": self.api_key, "regions": regions, "markets": markets, "oddsFormat": "decimal"},
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            self._cache.set(cache_key, data)
            events.extend(data)
        return events


def adapt_odds_api_event(event: dict) -> dict:
    """
    Z jednoho the-odds-api eventu vytáhne de-vigovanou pravděpodobnost pro
    match_winner a (pokud je dostupný) totals trh. Bere prvního bookmakera
    v odpovědi. Párování na zápas z jiného providera je tu jen přes přesnou
    shodu jména týmu/hráče (event["home_team"]/["away_team"]) — v produkci
    by chtělo robustnější fuzzy matching, jména se mezi providery často liší.
    """
    result = {
        "home_team": event["home_team"], "away_team": event["away_team"],
        "favorite_win_market_odds": None,
        "market_implied_probabilities": {},
        "over_threshold": None, "over_odds": None,
    }
    bookmakers = event.get("bookmakers", [])
    if not bookmakers:
        return result
    markets = bookmakers[0].get("markets", [])

    h2h = next((m for m in markets if m["key"] == "h2h"), None)
    if h2h:
        outcomes = [(o["name"], o["price"]) for o in h2h["outcomes"]]
        probs = devig_market(outcomes)
        home_name, away_name = event["home_team"], event["away_team"]
        if home_name in probs:
            result["market_implied_probabilities"]["match_winner:home"] = probs[home_name]
            result["favorite_win_market_odds"] = next(o["price"] for o in h2h["outcomes"] if o["name"] == home_name)
        if away_name in probs:
            result["market_implied_probabilities"]["match_winner:away"] = probs[away_name]

    totals = next((m for m in markets if m["key"] == "totals"), None)
    if totals:
        over_outcome = next((o for o in totals["outcomes"] if o["name"] == "Over"), None)
        under_outcome = next((o for o in totals["outcomes"] if o["name"] == "Under"), None)
        if over_outcome and under_outcome:
            p_over, _ = devig_two_way(over_outcome["price"], under_outcome["price"])
            result["over_threshold"] = over_outcome["point"]
            result["over_odds"] = over_outcome["price"]
            result["over_probability"] = p_over

    return result


# =======================================================================
# API-FOOTBALL — přímo přes api-sports.io (NE RapidAPI). Stejný klíč
# (APISPORTS_KEY) jako u Basketball/Hockey výše — jedna registrace na
# dashboard.api-sports.io zdarma odemkne i fotbal.
# Dokumentace: https://www.api-football.com/documentation-v3
# =======================================================================
import requests
from datetime import date, timedelta

API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"

# API-Football vyžaduje u /fixtures vždy alespoň jeden filtr (datum, liga+sezóna,
# tým...). Bez konkrétní ligy stahujeme den po dni přes 'date' parametr — funguje,
# ale je to dražší na počet requestů. Pro produkci doplň ID lig, které chceš
# sledovat (např. 39 = Premier League, 140 = La Liga, 78 = Bundesliga...),
# ať se dotazuje přímo přes league+season+next a šetří rate limit.
WATCHED_LEAGUE_IDS: list[int] = []


class APIFootballProvider(SportsDataProvider):
    def __init__(self, api_key: Optional[str] = None, cache_ttl_seconds: int = 30):
        self.api_key = api_key or os.environ.get("APISPORTS_KEY", "")
        if not self.api_key:
            raise RuntimeError(
                "Chybí APISPORTS_KEY. Tohle je klíč z dashboard.api-sports.io "
                "(stejný, co používáš pro hokej/basketbal) — nastav ho jako "
                "proměnnou prostředí na serveru, kde běží backend."
            )
        self._cache = InMemoryCache(ttl_seconds=cache_ttl_seconds)

    def _headers(self) -> dict:
        return {"x-apisports-key": self.api_key}

    def _get(self, path: str, params: dict) -> list:
        resp = requests.get(f"{API_FOOTBALL_BASE_URL}{path}", headers=self._headers(), params=params, timeout=8)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"API-Football vrátilo chybu: {payload['errors']}")
        return payload.get("response", [])

    def get_upcoming_matches(self, sport: Sport, days_ahead: int) -> list[dict]:
        if sport != Sport.FOOTBALL:
            raise NotImplementedError("APIFootballProvider pokrývá jen fotbal.")
        cache_key = f"upcoming:{days_ahead}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        fixtures: list[dict] = []
        if WATCHED_LEAGUE_IDS:
            for league_id in WATCHED_LEAGUE_IDS:
                fixtures.extend(self._get("/fixtures", {
                    "league": league_id, "season": date.today().year, "next": 15,
                }))
        else:
            today = date.today()
            for offset in range(days_ahead + 1):
                day = today + timedelta(days=offset)
                fixtures.extend(self._get("/fixtures", {"date": day.isoformat()}))

        self._cache.set(cache_key, fixtures)
        return fixtures

    def get_team_statistics(self, sport: Sport, team_id: str) -> dict:
        cache_key = f"team_stats:{team_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        # Pozn.: API-Football vyžaduje i 'league' parametr pro tuto endpoint v praxi
        # (statistika je vždy v kontextu konkrétní soutěže) — doplň dle zápasu.
        response = self._get("/teams/statistics", {"team": team_id, "season": date.today().year})
        data = response if isinstance(response, dict) else (response[0] if response else {})
        self._cache.set(cache_key, data)
        return data

    def get_pre_match_odds(self, match_id: str) -> dict:
        response = self._get("/odds", {"fixture": match_id})
        return response[0] if response else {}

    def get_live_match_stats(self, match_id: str) -> dict:
        stats = self._get("/fixtures/statistics", {"fixture": match_id})
        events = self._get("/fixtures/events", {"fixture": match_id})
        return {"statistics": stats, "events": events}

    def get_live_fixtures(self) -> list[dict]:
        """Vrátí všechny právě běžící zápasy — vstup pro poller Live Signal Engine."""
        return self._get("/fixtures", {"live": "all"})


# ---------------------------------------------------------------------
# Adaptéry: skutečný JSON tvar API-Football -> generické dicty, které
# normalize_to_match_input / normalize_to_match_snapshot (výše) očekávají.
# ---------------------------------------------------------------------
def adapt_api_football_fixture(fixture: dict) -> dict:
    """fixture = jeden prvek z `response` endpointu /fixtures."""
    return {
        "id": fixture["fixture"]["id"],
        "home_team": fixture["teams"]["home"]["name"],
        "away_team": fixture["teams"]["away"]["name"],
        "home_team_id": fixture["teams"]["home"]["id"],
        "away_team_id": fixture["teams"]["away"]["id"],
    }


def adapt_api_football_team_stats(stats: dict) -> dict:
    """stats = `response` objekt z /teams/statistics."""
    goals_avg = (
        stats.get("goals", {}).get("for", {}).get("average", {}).get("total", "1.2")
        or "1.2"
    )
    yellow_cards = stats.get("cards", {}).get("yellow", {})
    total_yellow = sum(
        int(v.get("total") or 0) for v in yellow_cards.values() if isinstance(v, dict)
    )
    played = stats.get("fixtures", {}).get("played", {}).get("total") or 1
    return {
        "avg_goals_scored_last_10": float(goals_avg),
        "avg_cards_last_10": round(total_yellow / played, 2),
    }


def adapt_api_football_odds(odds_response: dict, bookmaker_name: str = "Bet365") -> dict:
    """odds_response = jeden prvek `response` z /odds (pro daný fixture)."""
    result: dict = {"match_winner": {}, "over_goals": {}, "over_cards": {}}
    bookmakers = odds_response.get("bookmakers", [])
    if not bookmakers:
        return result
    target = next((b for b in bookmakers if b.get("name") == bookmaker_name), bookmakers[0])

    for bet in target.get("bets", []):
        name = bet.get("name")
        values = bet.get("values", [])
        if name == "Match Winner":
            home_odd = next((v["odd"] for v in values if v["value"] == "Home"), None)
            if home_odd:
                result["match_winner"]["favorite"] = float(home_odd)
        elif name == "Goals Over/Under":
            for v in values:
                if str(v["value"]).startswith("Over "):
                    threshold = float(v["value"].replace("Over ", ""))
                    result["over_goals"][threshold] = float(v["odd"])
        elif name == "Cards Over/Under":
            for v in values:
                if str(v["value"]).startswith("Over "):
                    threshold = float(v["value"].replace("Over ", ""))
                    result["over_cards"][threshold] = float(v["odd"])
    return result


def adapt_api_football_live_stats(minute: int, live_raw: dict) -> dict:
    """
    live_raw = {'statistics': [...], 'events': [...]} z get_live_match_stats().

    Pozn.: API-Football nemá nativní metriku 'dangerous attacks' (na rozdíl
    od Sportmonks/SofaScore). Jako proxy používáme 'Shots insidebox' — není
    to identické, ale koreluje s reálným ohrožením branky lépe než např.
    celkový počet střel. Pokud chceš přesnější metriku, zvaž doplňkové
    API specificky pro 'momentum'/'attack danger' data.
    """
    stats_by_team: dict[str, dict] = {}
    for team_block in live_raw.get("statistics", []):
        team_name = team_block["team"]["name"]
        stats_by_team[team_name] = {
            item["type"]: item["value"] for item in team_block.get("statistics", [])
        }

    teams = list(stats_by_team.keys())
    home_name, away_name = (teams[0], teams[1]) if len(teams) == 2 else (None, None)
    home_stats = stats_by_team.get(home_name, {})
    away_stats = stats_by_team.get(away_name, {})

    def _num(v):
        if v is None:
            return 0
        if isinstance(v, str) and v.endswith("%"):
            return int(v.replace("%", "") or 0)
        return int(v) if v else 0

    red_cards_home = sum(
        1 for e in live_raw.get("events", [])
        if e.get("type") == "Card" and e.get("detail") == "Red Card" and e.get("team", {}).get("name") == home_name
    )
    red_cards_away = sum(
        1 for e in live_raw.get("events", [])
        if e.get("type") == "Card" and e.get("detail") == "Red Card" and e.get("team", {}).get("name") == away_name
    )

    return {
        "minute": minute,
        "possession": {"home": _num(home_stats.get("Ball Possession")), "away": _num(away_stats.get("Ball Possession"))},
        "shots_on_target": {"home": _num(home_stats.get("Shots on Goal")), "away": _num(away_stats.get("Shots on Goal"))},
        "dangerous_attacks": {"home": _num(home_stats.get("Shots insidebox")), "away": _num(away_stats.get("Shots insidebox"))},
        "corners": {"home": _num(home_stats.get("Corner Kicks")), "away": _num(away_stats.get("Corner Kicks"))},
        "red_cards": {"home": red_cards_home, "away": red_cards_away},
    }


# =======================================================================
# Factory — vybere providera dle sportu (definováno až tady, na konci,
# protože potřebuje znát všechny třídy výše)
# =======================================================================
def get_provider(sport: Sport) -> SportsDataProvider:
    if sport == Sport.FOOTBALL:
        return APIFootballProvider()
    if sport == Sport.BASKETBALL:
        return APISportsDirectProvider(sport_path="basketball")
    if sport == Sport.HOCKEY:
        return APISportsDirectProvider(sport_path="hockey")
    if sport == Sport.TENNIS:
        return APITennisProvider()
    raise NotImplementedError(f"Pro sport '{sport.value}' chybí napojený provider.")
