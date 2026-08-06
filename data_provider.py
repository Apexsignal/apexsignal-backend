"""
ApexSignal — Integrační vrstva pro sportovní data a kurzy
Modul: data_provider.py

Účel:
    Abstrahuje konkrétního API providera (např. API-Football, Sportradar,
    Betfair Exchange, Pinnacle API...) za jednotné rozhraní, které
    `probability_model.py` konzumuje bez znalosti konkrétního externího
    kontraktu.

    Obsahuje:
      - SportsDataProvider: abstraktní rozhraní
      - HttpSportsDataProvider: referenční implementace přes obecné REST API
      - InMemoryCache: jednoduchý TTL cache layer (omezuje počet API callů)
      - normalizační funkce -> MatchInput (pro generátor tiketů)

    Pozn.: Reálné API klíče se dosazují přes proměnné prostředí (APISPORTS_KEY,
    APITENNIS_KEY, ODDSAPI_KEY) — nastav je na serveru, kde poběží backend —
    nikdy ne v kódu ani ve frontend souborech.
"""

from __future__ import annotations

import os
import re
import time
import threading
import unicodedata
import difflib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from probability_model import MatchInput, Sport, MarketType, devig_market, devig_two_way, MarketEvaluator, HT_GOAL_SHARE


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


# ---------------------------------------------------------------------
# Referenční HTTP implementace (obecná, použitelná pro většinu REST API
# typu API-Football / Sportmonks / Sportradar po doplnění mapování polí)
# ---------------------------------------------------------------------
class HttpSportsDataProvider(SportsDataProvider):
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 cache_ttl_seconds: int = 300):
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


# ---------------------------------------------------------------------
# Normalizace: raw API response -> interní datové struktury
# ---------------------------------------------------------------------
def normalize_to_match_input(
    sport: Sport,
    fixture: dict,
    home_stats: dict,
    away_stats: dict,
    odds_raw: dict,
    home_recent_form: Optional[float] = None,
    away_recent_form: Optional[float] = None,
    weather: Optional[dict] = None,
    home_injury_count: int = 0,
    away_injury_count: int = 0,
    home_rest_days: Optional[int] = None,
    away_rest_days: Optional[int] = None,
    home_dead_rubber: float = 1.0,
    away_dead_rubber: float = 1.0,
    data_availability: Optional[dict] = None,
) -> MatchInput:
    """
    Převede syrová data z providera na MatchInput konzumovaný
    probability_model.TicketGenerator. Mapování klíčů (`fixture["..."]`)
    je třeba upravit dle konkrétního API kontraktu.
    """
    weather_factor = weather_goal_adjustment_factor(weather)
    home_factor = weather_factor * injury_goal_adjustment_factor(home_injury_count) \
        * rest_days_adjustment_factor(home_rest_days) * home_dead_rubber
    away_factor = weather_factor * injury_goal_adjustment_factor(away_injury_count) \
        * rest_days_adjustment_factor(away_rest_days) * away_dead_rubber
    # Domácí góly appka počítá i podle HOSTOVA obranného průměru (a naopak)
    # — viz opponent_stats v _estimate_expected_goals.
    home_xg = _estimate_expected_goals(
        home_stats, is_home=True, recency_weighted_avg=home_recent_form,
        adjustment_factor=home_factor, opponent_stats=away_stats,
    )
    away_xg = _estimate_expected_goals(
        away_stats, is_home=False, recency_weighted_avg=away_recent_form,
        adjustment_factor=away_factor, opponent_stats=home_stats,
    )
    expected_cards = _estimate_expected_cards(home_stats, away_stats)

    # Appka DŘÍV bez reálného kurzu cenu VYMÝŠLELA z vlastního modelu
    # (1/pravděpodobnost) — živě potvrzený problém důvěryhodnosti
    # (2026-08-06, uživatel dohledal rozdíly 2-50 % proti reálným
    # Tipsport cenám u zápasu Vlašim/PAOK/Alianza). Appka teď bez
    # skutečného tržního kurzu MATCH_WINNER kandidáta vůbec nenabídne
    # (favorite_odds_verified=False, viz build_candidates) — placeholder
    # 2.0 appka nechává jen ať MatchInput má syntakticky platnou hodnotu
    # (dataclass pole není Optional), na výběr kandidátů nemá žádný vliv.
    favorite_odds = odds_raw.get("match_winner", {}).get("favorite", None)
    favorite_odds_verified = favorite_odds is not None and favorite_odds >= 1.01
    if not favorite_odds_verified:
        favorite_odds = 2.0

    return MatchInput(
        match_id=fixture["id"],
        sport=sport,
        home_team=fixture["home_team"],
        away_team=fixture["away_team"],
        league=fixture.get("league", ""),
        country=fixture.get("country", ""),
        league_id=fixture.get("league_id"),
        kickoff_date=(fixture.get("kickoff_time") or "")[:10],  # jen datum (YYYY-MM-DD) z ISO timestampu
        kickoff_time=(fixture.get("kickoff_time") or "")[11:16],  # čas HH:MM z ISO timestampu
        home_expected_goals=home_xg,
        away_expected_goals=away_xg,
        home_expected_goals_ht=round(home_xg * HT_GOAL_SHARE, 3) if home_xg else 0.0,
        away_expected_goals_ht=round(away_xg * HT_GOAL_SHARE, 3) if away_xg else 0.0,
        expected_cards=expected_cards,
        home_games_played=home_stats.get("games_played", 0),
        away_games_played=away_stats.get("games_played", 0),
        home_recent_form_available=home_recent_form is not None,
        away_recent_form_available=away_recent_form is not None,
        referee=fixture.get("referee"),
        weather_wind_kmh=(weather or {}).get("wind_speed_kmh"),
        weather_precipitation_mm=(weather or {}).get("precipitation_mm"),
        home_injury_count=home_injury_count,
        away_injury_count=away_injury_count,
        home_rest_days=home_rest_days,
        away_rest_days=away_rest_days,
        home_dead_rubber=home_dead_rubber,
        away_dead_rubber=away_dead_rubber,
        favorite_win_market_odds=favorite_odds,
        favorite_odds_verified=favorite_odds_verified,
        over_goals_odds=odds_raw.get("over_goals", {}),     # {2.5: 1.85, 3.5: 2.60, ...}
        under_goals_odds=odds_raw.get("under_goals", {}),   # {2.5: 1.95, ...} — skutečné tržní kurzy
        btts_yes_odds=odds_raw.get("btts_yes"),
        over_cards_odds=odds_raw.get("over_cards", {}),     # {3.5: 1.90, 4.5: 2.40, ...}
        # Market-consensus pravděpodobnosti spočítané z mediánu napříč VŠEMI
        # bookmakery v odpovědi (viz adapt_api_football_odds) — appka tím
        # má tržní kontrolu i bez druhého (the-odds-api) zdroje dat; pokud
        # je i ten k dispozici, _enrich_with_market_odds tyhle hodnoty
        # později ještě přepíše svými (the-odds-api agreguje přes ještě
        # víc bookmakerů, takže má přednost).
        market_implied_probabilities=dict(odds_raw.get("market_implied_probabilities", {})),
        data_availability=data_availability or {},
        market_odds_bookmaker_count=odds_raw.get("bookmaker_count"),
    )




LEAGUE_AVERAGE_GOALS_PER_TEAM = 1.3  # rozumný univerzální odhad přes evropské ligy
SHRINKAGE_PSEUDO_GAMES = 5  # kolik "fiktivních" zápasů váží ligový průměr vůči datům týmu
RECENCY_BLEND_WEIGHT = 0.6  # váha posledních zápasů vs. sezónního průměru — ustálený stav (dost odehraných zápasů)
RECENCY_BLEND_WEIGHT_MAX = 0.85  # strop váhy formy napříč sezónami, i při 0 odehraných zápasech téhle sezóny

# ---------------------------------------------------------------------
# Počasí — Open-Meteo (zdarma, bez API klíče, bez platební karty,
# 10 000 dotazů/den). API-Football nedává přímo souřadnice stadionu, jen
# název města, takže ho nejdřív zdarma "zeměpisně" přeložíme (geokódování)
# a teprve pro ty souřadnice stáhneme předpověď na čas výkopu.
# Efekt počasí na góly je menší než kvalita týmů/forma — faktor je
# proto jen mírný (max ~10-15% snížení za opravdu extrémních podmínek).
# ---------------------------------------------------------------------
OPEN_METEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

_geocode_cache: dict[str, Optional[tuple]] = {}  # město -> (lat, lon); kešováno navždy, města se nehýbou


def _geocode_city(city: str) -> Optional[tuple]:
    if not city:
        return None
    if city in _geocode_cache:
        return _geocode_cache[city]
    try:
        resp = requests.get(OPEN_METEO_GEOCODE_URL, params={"name": city, "count": 1}, timeout=5)
        resp.raise_for_status()
        results = resp.json().get("results")
        coords = (results[0]["latitude"], results[0]["longitude"]) if results else None
    except Exception:
        coords = None
    _geocode_cache[city] = coords
    return coords


def get_match_weather(venue_city: Optional[str], kickoff_iso: Optional[str]) -> Optional[dict]:
    """
    Vrátí {"wind_speed_kmh": ..., "precipitation_mm": ...} pro dané město
    v čase výkopu, nebo None (chybí město/čas, geokódování selhalo,
    výpadek API...) — appka se v takovém případě chová jako dřív,
    žádná korekce.
    """
    if not venue_city or not kickoff_iso:
        return None
    coords = _geocode_city(venue_city)
    if coords is None:
        return None
    lat, lon = coords
    try:
        resp = requests.get(OPEN_METEO_FORECAST_URL, params={
            "latitude": lat, "longitude": lon,
            "hourly": "precipitation,wind_speed_10m",
            "timezone": "UTC",
        }, timeout=5)
        resp.raise_for_status()
        hourly = resp.json().get("hourly", {})
        times = hourly.get("time", [])
        target_hour = kickoff_iso[:13]  # "YYYY-MM-DDTHH" — najdeme nejbližší hodinu k výkopu
        for i, t in enumerate(times):
            if t.startswith(target_hour):
                return {
                    "wind_speed_kmh": hourly["wind_speed_10m"][i],
                    "precipitation_mm": hourly["precipitation"][i],
                }
        return None
    except Exception:
        return None


def weather_goal_adjustment_factor(weather: Optional[dict]) -> float:
    """
    Multiplikativní faktor (<=1.0) na expected goals podle počasí. Silný
    vítr a déšť typicky snižují počet gólů (těžší kontrola míče, méně
    přesné centry/střely). Bez dat o počasí vrací 1.0 = beze změny.
    """
    if weather is None:
        return 1.0
    factor = 1.0
    wind = weather.get("wind_speed_kmh", 0) or 0
    rain = weather.get("precipitation_mm", 0) or 0
    if wind > 30:
        factor *= 0.96
    if wind > 50:
        factor *= 0.96
    if rain > 2:
        factor *= 0.97
    if rain > 8:
        factor *= 0.96
    return factor


def injury_goal_adjustment_factor(injury_count: int) -> float:
    """
    Multiplikativní faktor (<=1.0) podle počtu hráčů nahlášených jako
    zranění/vyloučení pro tenhle konkrétní zápas (viz get_injuries).
    POZOR: appka nerozlišuje hvězdu základní sestavy od náhradníka na
    konci lavičky — endpoint /injuries vrací jména, ne důležitost hráče
    pro tým. Proto je dopad na hráče mírný a s tvrdým stropem — appka
    raději podcení dopad zranění, než aby na základě neúplné informace
    "vyhodila" tým z modelu úplně.
    """
    DAMPEN_PER_PLAYER = 0.03
    MAX_TOTAL_DAMPEN = 0.20  # i 10 nahlasenych jmen appku neposune pod 80 % puvodniho xG
    factor = max(1.0 - injury_count * DAMPEN_PER_PLAYER, 1.0 - MAX_TOTAL_DAMPEN)
    return factor


def adapt_injuries(injuries_raw: list[dict], team_name: str) -> int:
    """
    Spočítá počet nahlášených zranění/vyloučení pro konkrétní tým z
    odpovědi /injuries (viz ApiFootballProvider.get_injuries) — appka
    z toho umí jen POČET jmen, ne jejich důležitost pro tým, viz
    injury_goal_adjustment_factor.

    Appka backend_api.py volala data_provider.adapt_injuries(...), ale
    tahle funkce v data_provider.py nikdy neexistovala (stejná třída
    bugu jako u adapt_rest_days) — appka to tiše odchytávala přes
    except Exception a vždy dosadila home_injury_count=away_injury_count=0,
    takže zranění/vyloučení appka nikdy reálně nepromítala do xG odhadu.
    """
    return sum(1 for entry in injuries_raw if (entry.get("team") or {}).get("name") == team_name)


def rest_days_adjustment_factor(days_since_last_match: Optional[int]) -> float:
    """
    Multiplikativní faktor (<=1.0) podle počtu dní od posledního zápasu
    týmu. Krátký odpočinek (typicky čtvrtek pohár -> neděle liga) je
    dobře zdokumentovaný únavový efekt. Bez dat appka vrací 1.0.
    """
    if days_since_last_match is None:
        return 1.0
    if days_since_last_match <= 2:
        return 0.93   # dva dny odpočinku a méně — výrazná únava
    if days_since_last_match <= 3:
        return 0.96   # tři dny — mírná únava
    return 1.0


# Nastavení pro konkrétní ligy — počet sestupových a evropských míst.
# Appka tyhle hodnoty neodhaduje, jsou to pevná pravidla daných soutěží.
# Klíč = league_id z API-Football.
_LEAGUE_CONFIG: dict[int, dict] = {
    39:  {"name": "Premier League",       "relegation": 3, "europe": 6, "teams": 20},
    40:  {"name": "Championship",          "relegation": 3, "europe": 6, "teams": 24},
    78:  {"name": "Bundesliga",            "relegation": 2, "europe": 7, "teams": 18},  # 2 přímý + 1 baráž
    135: {"name": "Serie A",               "relegation": 3, "europe": 7, "teams": 20},
    140: {"name": "La Liga",               "relegation": 3, "europe": 6, "teams": 20},
    61:  {"name": "Ligue 1",               "relegation": 3, "europe": 6, "teams": 18},
    88:  {"name": "Eredivisie",            "relegation": 3, "europe": 6, "teams": 18},
    94:  {"name": "Primeira Liga",         "relegation": 3, "europe": 5, "teams": 18},
    144: {"name": "Jupiler Pro League",    "relegation": 3, "europe": 4, "teams": 16},
    203: {"name": "Süper Lig",            "relegation": 3, "europe": 5, "teams": 19},
    235: {"name": "Russian Premier League","relegation": 2, "europe": 4, "teams": 16},
    307: {"name": "Saudi Pro League",      "relegation": 3, "europe": 3, "teams": 18},
    # Skandinávské ligy (letní sezóna = přesně kdy appka běží)
    103: {"name": "Eliteserien",           "relegation": 2, "europe": 3, "teams": 16},
    113: {"name": "Allsvenskan",           "relegation": 2, "europe": 3, "teams": 16},
    244: {"name": "Veikkausliiga",         "relegation": 2, "europe": 2, "teams": 12},
}

_DEFAULT_LEAGUE_CONFIG = {"relegation": 3, "europe": 6, "teams": 18}


def adapt_standings_for_motivation(
    standings: list[dict],
    team_name: str,
    league_id: Optional[int] = None,
    games_remaining_threshold: int = 8,
) -> float:
    """
    Vrací spojitý faktor motivace týmu (0.82 – 1.10) místo starého bool.
    Hodnota 1.0 = normální motivace. Výrazně pod 1.0 = tým nemá o co hrát.
    Výrazně nad 1.0 = tým hraje o hodně (titul, záchrana, Evropa).

    Appka rozlišuje čtyři situace:
    - Boj o titul / první místo → +10 % (vyšší intenzita, plná sestava)
    - Boj o Evropu / záchranná baráž → +5 %
    - Neutrální střed tabulky → 0 % (normální)
    - Jistý střed bez motivace (dead rubber) → -12 % (rotace sestavy)

    Appka při jakékoli nejistotě (chybějící tabulka, tým nenalezen)
    vrací 1.0 — raději efekt podcení, než aby ho vymyslela.
    """
    if not standings:
        return 1.0

    team_row = next(
        (row for row in standings if row.get("team", {}).get("name") == team_name),
        None,
    )
    if not team_row:
        return 1.0

    cfg = _LEAGUE_CONFIG.get(league_id or 0, _DEFAULT_LEAGUE_CONFIG)
    total_teams = len(standings)
    total_games = (total_teams - 1) * 2
    played = team_row.get("all", {}).get("played", 0)
    games_remaining = total_games - played

    if games_remaining < 0 or played == 0:
        return 1.0

    sorted_standings = sorted(standings, key=lambda r: r.get("rank", 999))
    team_points = team_row.get("points", 0)
    team_rank = team_row.get("rank", total_teams // 2)
    max_possible_points = team_points + games_remaining * 3

    # Šance na titul
    leader_points = sorted_standings[0].get("points", 0) if sorted_standings else team_points
    can_win_title = max_possible_points >= leader_points

    # Šance na Evropu
    europe_cutoff = sorted_standings[cfg["europe"] - 1].get("points", 0) if len(sorted_standings) >= cfg["europe"] else 9999
    can_reach_europe = max_possible_points >= europe_cutoff

    # Ohrožení sestupu
    relegation_border = total_teams - cfg["relegation"]
    safe_cutoff = sorted_standings[relegation_border - 1].get("points", 0) if len(sorted_standings) > relegation_border else 0
    points_above_drop = team_points - safe_cutoff
    can_be_relegated = points_above_drop <= games_remaining * 3

    # Výpočet faktoru
    if games_remaining <= games_remaining_threshold:
        # Konec sezóny — kontexty se zjednodušují
        if can_win_title and team_rank == 1:
            return 1.10  # boj o titul
        if can_win_title and team_rank <= 3:
            return 1.07
        if can_reach_europe and team_rank <= cfg["europe"]:
            return 1.05  # boj o Evropu
        if can_be_relegated and points_above_drop <= 6:
            return 1.08  # záchranná baráž — vysoká intenzita
        if can_be_relegated:
            return 1.04
        # Dead rubber — tým nemá o co hrát
        points_gap_to_nearest_target = min(
            abs(europe_cutoff - team_points),
            abs(points_above_drop - 12),
        )
        if points_gap_to_nearest_target >= 15:
            return 0.82  # jasný střed tabulky bez motivace
        if points_gap_to_nearest_target >= 8:
            return 0.90
        return 1.0  # nejasná situace — neutrální
    else:
        # Sezóna ještě běží — motivace je obecně vyšší, ale stále relevantní
        if can_win_title and team_rank <= 2:
            return 1.07
        if can_be_relegated and points_above_drop <= 3:
            return 1.07  # reálné ohrožení sestupu
        if not can_reach_europe and not can_be_relegated and games_remaining <= 5:
            return 0.90  # brzy konec, nic v sázce
        return 1.0


def motivation_adjustment_factor(motivation_factor: float) -> float:
    """Přechodový wrapper — nová verze adapt_standings_for_motivation vrací
    přímo faktor (float), starý kód předával bool. Tato funkce zajistí
    zpětnou kompatibilitu, pokud by někde v kódu ještě byl bool."""
    if isinstance(motivation_factor, bool):
        return 0.90 if motivation_factor else 1.0
    return motivation_factor


def adapt_rest_days(recent_fixtures: list[dict], kickoff_iso: str) -> Optional[int]:
    """
    Počet dní od posledního odehraného zápasu týmu do tohoto výkopu —
    appka to počítá z dat, co už tahá pro recency formu (get_recent_form),
    žádný extra API dotaz navíc. Vrací None, pokud appka nemá dostatek dat.

    Byla tu jen jako mrtvý, nikdy nevolaný kód (omylem vložený do těla
    motivation_adjustment_factor při jednom z dřívějších sloučení) —
    backend_api.py přitom volá data_provider.adapt_rest_days(...), který
    díky tomu reálně neexistoval a při KAŽDÉM zápase spadl na
    AttributeError. To by samo o sobě nevadilo (volající kód to odchytává
    přes except Exception), jenže stejný except blok kvůli tomu zahazoval
    i už úspěšně spočítanou home_form/away_form ze stejné try sekce —
    appka tak fakticky nikdy nepoužívala váženou nedávnou formu, odkdy
    byl tenhle blok přidán (2026-07-21).
    """
    if not recent_fixtures:
        return None
    try:
        last_match_date = datetime.fromisoformat(recent_fixtures[0]["fixture"]["date"].replace("Z", "+00:00"))
        kickoff_date = datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00"))
        return max((kickoff_date - last_match_date).days, 0)
    except (KeyError, ValueError, TypeError, IndexError):
        return None


DEFENSE_FACTOR_MIN = 0.6  # appka obranou sílu soupeře omezí na tohle rozpětí — i tým
DEFENSE_FACTOR_MAX = 1.6  # s extrémní (často málo podloženou) obranou nesmí xG zlomit


def _estimate_expected_goals(
    team_stats: dict, is_home: bool, recency_weighted_avg: Optional[float] = None,
    adjustment_factor: float = 1.0, opponent_stats: Optional[dict] = None,
) -> float:
    """
    Zjednodušený xG odhad: průměr vstřelených gólů, upravený o domácí/
    venkovní výhodu a o obrannou sílu SOUPEŘE, se čtyřmi vrstvami
    opatrnosti navrch:

    1) Shrinkage na malém vzorku — na začátku sezóny (málo odehraných
       zápasů) je sezónní průměr statisticky nespolehlivý (velký šum).
       "Stáhneme" ho blíž k ligovému průměru úměrně tomu, kolik dat tým
       má; s přibývajícími zápasy korekce postupně mizí. Appka stejnou
       opatrnost aplikuje i na obranou sílu soupeře.
    2) Vážení nedávné formy — pokud `recency_weighted_avg` je dostupný
       (poslední zápasy vážené víc než starší, viz data_provider.
       adapt_recent_form_goals), zkombinuje se se sezónním průměrem,
       aby appka reagovala na aktuální formu, ne jen na celosezónní stav.
    3) opponent_stats — appka dřív počítala góly týmu jen z JEHO
       VLASTNÍHO útočného průměru a soupeřovu obranu úplně ignorovala,
       i když appka ta data (goals.against z /teams/statistics) z API
       dostávala celou dobu, jen je zahazovala (viz
       adapt_api_football_team_stats). Tým se sezónním průměrem
       "2 góly/zápas" proti nejlepší obraně ligy reálně dá míň, než
       proti nejhorší — appka teď avg_goals_conceded_last_10 soupeře
       promítne jako multiplikátor (poměr k ligovému průměru), stejnou
       shrinkage logikou jako u vlastního útoku.
    4) adjustment_factor — souhrnný multiplikátor počasí × zranění ×
       odpočinku × motivace (viz *_adjustment_factor funkce výše); bez
       jakýchkoli dat zůstává 1.0 = beze změny.

    V produkci by šlo nahradit plnohodnotným Dixon-Coles modelem
    (odhad útočné/obranné síly regresí přes celou ligovou tabulku, ne
    jen pár týmu) — to by vyžadovalo samostatný (a placený) zdroj dat.
    Tohle je levnější mezikrok se stejnými daty, co appka už beztak má.
    """
    avg_goals_scored = team_stats.get("avg_goals_scored_last_10", 1.2)
    games_played = team_stats.get("games_played", 0)

    shrunk_avg = (
        games_played * avg_goals_scored + SHRINKAGE_PSEUDO_GAMES * LEAGUE_AVERAGE_GOALS_PER_TEAM
    ) / (games_played + SHRINKAGE_PSEUDO_GAMES)

    if recency_weighted_avg is not None:
        # Na začátku sezóny (málo odehraných zápasů) appka dřív pořád
        # vážila formu napříč sezónami pevným poměrem 60/40 — tím zbytečně
        # ředila spolehlivý zdroj (recency_weighted_avg, appka ho má u
        # skoro všech týmů) tím nespolehlivým (pár zápasů týhle sezóny,
        # co appka navíc už samo o sobě stahuje k ligovému průměru přes
        # shrinkage výše). Váha formy napříč sezónami proto teď roste,
        # čím míň má tahle sezóna odehraných zápasů — a s přibývajícími
        # zápasy se přirozeně vrací k ustálenému poměru RECENCY_BLEND_WEIGHT.
        recency_weight = max(
            RECENCY_BLEND_WEIGHT,
            RECENCY_BLEND_WEIGHT_MAX - games_played * (RECENCY_BLEND_WEIGHT_MAX - RECENCY_BLEND_WEIGHT) / SHRINKAGE_PSEUDO_GAMES,
        )
        shrunk_avg = recency_weight * recency_weighted_avg + (1 - recency_weight) * shrunk_avg

    defense_factor = 1.0
    if opponent_stats is not None:
        opp_conceded = opponent_stats.get("avg_goals_conceded_last_10")
        if opp_conceded is not None:
            opp_games = opponent_stats.get("games_played", 0)
            shrunk_opp_conceded = (
                opp_games * opp_conceded + SHRINKAGE_PSEUDO_GAMES * LEAGUE_AVERAGE_GOALS_PER_TEAM
            ) / (opp_games + SHRINKAGE_PSEUDO_GAMES)
            defense_factor = shrunk_opp_conceded / LEAGUE_AVERAGE_GOALS_PER_TEAM
            defense_factor = min(max(defense_factor, DEFENSE_FACTOR_MIN), DEFENSE_FACTOR_MAX)

    home_advantage_factor = 1.10 if is_home else 0.92
    return round(shrunk_avg * home_advantage_factor * adjustment_factor * defense_factor, 2)


def adapt_recent_form_goals(fixtures: list[dict], team_id: int, venue: Optional[str] = None) -> Optional[float]:
    """
    Z posledních N zápasů (raw /fixtures?team=X&last=N&status=FT) spočítá
    vážený průměr vstřelených gólů — nejnovější zápas váží nejvíc, nejstarší
    nejméně (lineární váhy 1..N). Vrací None, pokud appka žádné dokončené
    zápasy nedostala (nový tým v lize, výpadek API...).

    venue: "home" / "away" / None. Forma týmu doma a venku se prokazatelně
    liší (jeden z nejlépe podložených efektů ve fotbalové analytice) — při
    zadání appka spočítá formu jen z zápasů na daném prostředí. Pokud by
    po filtrování zbylo míň než MIN_VENUE_SPLIT_SAMPLES zápasů (např. tým
    odehrál v posledních N jen 1 zápas doma), appka se bezpečně vrátí
    k nefiltrovanému průměru ze všech zápasů — širší vzorek s větším
    šumem je lepší než úzký vzorek s extrémním šumem.
    """
    MIN_VENUE_SPLIT_SAMPLES = 2
    goals: list[int] = []
    for fx in fixtures:
        home_id = fx["teams"]["home"]["id"]
        is_home = home_id == team_id
        if venue == "home" and not is_home:
            continue
        if venue == "away" and is_home:
            continue
        scored = fx["goals"]["home"] if is_home else fx["goals"]["away"]
        if scored is not None:
            goals.append(scored)

    if venue is not None and len(goals) < MIN_VENUE_SPLIT_SAMPLES:
        return adapt_recent_form_goals(fixtures, team_id, venue=None)

    if not goals:
        return None
    # API vrací poslední zápasy nejnovější první — otočíme, ať nejnovější
    # dostane nejvyšší váhu v lineárním vážení.
    goals = list(reversed(goals))
    weights = list(range(1, len(goals) + 1))
    weighted_sum = sum(g * w for g, w in zip(goals, weights))
    return round(weighted_sum / sum(weights), 2)


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
    def __init__(self, sport_path: str, api_key: Optional[str] = None, cache_ttl_seconds: int = 300):
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
            time.sleep(0.3)  # malá pauza mezi requesty — šetří limit a vypadá to méně jako scraping
        self._cache.set(cache_key, games)
        return games

    def get_team_statistics(self, sport: Sport, team_id: str) -> dict:
        cache_key = f"team_stats:{team_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        # Pozn.: v praxi tahle endpoint často vyžaduje i 'league' parametr —
        # doplň ID ligy, kterou sleduješ (appka u fotbalu místo toho bere
        # league_id přímo z konkrétního zápasu — viz get_team_statistics
        # v APIFootballProvider, appka netáhne přes pevný seznam lig).
        season = _current_season_string(hyphenated=True)
        response = self._get("/teams/statistics", {"team": team_id, "season": season})
        data = response if isinstance(response, dict) else (response[0] if response else {})
        self._cache.set(cache_key, data)
        return data

    def get_pre_match_odds(self, match_id: str) -> dict:
        response = self._get("/odds", {"game": match_id})
        return response[0] if response else {}


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

    def __init__(self, api_key: Optional[str] = None, cache_ttl_seconds: int = 300):
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
    # vlastní sport_key. Seznam appka drží v souladu s TIPSPORT_LEAGUE_IDS
    # (soutěže appka vybírá zápasy z nich) — předtím appka měla nastavené
    # jen 2 soutěže (EPL, Liga mistrů), takže naprostá většina appkou
    # vybraných zápasů neměla žádný skutečný tržní kurz k porovnání a
    # appka si kurz dopočítávala sama ze svého odhadu. Seznam appka
    # ověřila živě přes /v4/sports, ne podle zastaralé dokumentace.
    # Zahrnuté evropské poháry appka teď (mimo sezónu) může vracet
    # prázdné, na podzim se ale appce rozjedou znovu.
    # 2026-07-26: appka ověřila živě přes /v4/sports, že aktuálně nabízených
    # fotbalových klíčů je celkem jen 40 — spousta zemí z TIPSPORT_LEAGUE_IDS
    # (Rumunsko, Česko, Turecko, Chorvatsko, Srbsko, Maďarsko, Slovensko,
    # Kypr, Izrael, Uruguay, Kolumbie, Japonsko, Austrálie, Saúdská Arábie,
    # mezinárodní kvalifikace) tam PROSTĚ NENÍ — appka o ně the-odds-api
    # nemůže požádat, protože appka je nemá, ne kvůli chybě v párování.
    # Portugalsko a italská Serie B ale live dostupné byly a appka je
    # předtím neměla nastavené (2 kredity navíc, zanedbatelné vůči rozpočtu
    # 500/měsíc, viz get_odds níže).
    SPORT_KEYS: dict[Sport, list[str]] = {
        Sport.FOOTBALL: [
            "soccer_epl", "soccer_efl_champ", "soccer_england_league1", "soccer_england_league2",
            "soccer_england_efl_cup",
            "soccer_germany_bundesliga", "soccer_germany_bundesliga2", "soccer_germany_liga3", "soccer_germany_dfb_pokal",
            "soccer_italy_serie_a", "soccer_italy_serie_b",
            "soccer_spain_la_liga",
            "soccer_portugal_primeira_liga",
            "soccer_france_ligue_one",
            "soccer_netherlands_eredivisie",
            "soccer_belgium_first_div",
            "soccer_spl",
            "soccer_switzerland_superleague",
            "soccer_sweden_allsvenskan", "soccer_sweden_superettan",
            "soccer_norway_eliteserien",
            "soccer_denmark_superliga",
            "soccer_finland_veikkausliiga",
            "soccer_russia_premier_league",
            "soccer_poland_ekstraklasa",
            "soccer_brazil_campeonato", "soccer_brazil_serie_b",
            "soccer_argentina_primera_division",
            "soccer_chile_campeonato",
            "soccer_usa_mls",
            "soccer_mexico_ligamx",
            "soccer_korea_kleague1",
            "soccer_china_superleague",
            "soccer_league_of_ireland",
            "soccer_austria_bundesliga",
            "soccer_greece_super_league",
            "soccer_uefa_champs_league", "soccer_uefa_champs_league_qualification",
            "soccer_uefa_europa_league", "soccer_uefa_europa_conference_league",
            "soccer_conmebol_copa_libertadores", "soccer_conmebol_copa_sudamericana",
        ],
        Sport.BASKETBALL: ["basketball_nba"],
        Sport.HOCKEY: ["icehockey_nhl"],
        Sport.TENNIS: [],  # turnajové klíče se mění (např. "tennis_atp_french_open") — doplň aktuální
    }

    def __init__(self, api_key: Optional[str] = None, cache_ttl_seconds: int = 300):
        self.api_key = api_key or os.environ.get("ODDSAPI_KEY", "")
        if not self.api_key:
            raise RuntimeError("Chybí ODDSAPI_KEY (proměnná prostředí).")
        self._cache = InMemoryCache(ttl_seconds=cache_ttl_seconds)

    # Nový klíč má rozpočet jen 500 KREDITŮ/MĚSÍC (ne za den!) a appka bere
    # 2 trhy (h2h+totals) × 1 region (eu) = 2 kredity za jeden request.
    # Jeden plný průchod ~35 lig tak stojí ~70 kreditů — při refreshi
    # každé 4h by appka vyčerpala měsíční kvótu za 2-3 DNY. Appka proto
    # kešuje kurzy do DB na celý týden — sdíleno napříč VŠEMI požadavky a
    # přežije restart. 35 lig × ~4,3 obnovení/měsíc × 2 kredity ≈ 300
    # kreditů/měsíc, což nechává rozumnou rezervu.
    DB_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60

    def get_odds(self, sport: Sport, markets: str = "h2h,totals", regions: str = "eu") -> list[dict]:
        """
        Chyba na JEDNÉ lize (výpadek, došlá kvóta...) nesmí shodit celé
        generování tiketu — appka takovou ligu jen přeskočí a jede dál.
        Při 401 (neplatný klíč nebo došlá kvóta) appka navíc rovnou
        ukončí celou smyčku — další ligy by selhaly úplně stejně, nemá
        smysl na ně plýtvat dalšími voláními.
        """
        events: list[dict] = []
        for sport_key in self.SPORT_KEYS.get(sport, []):
            cache_key = f"odds:{sport_key}:{markets}"
            cached = self._cache.get(cache_key)
            if cached is not None:
                events.extend(cached)
                continue

            try:
                import db as _db
                db_cached = _db.cache_get(cache_key)
            except Exception:
                db_cached = None
            if db_cached is not None:
                self._cache.set(cache_key, db_cached)
                events.extend(db_cached)
                continue

            try:
                resp = requests.get(
                    f"{self.BASE_URL}/sports/{sport_key}/odds",
                    params={"apiKey": self.api_key, "regions": regions, "markets": markets, "oddsFormat": "decimal"},
                    timeout=8,
                )
                resp.raise_for_status()
            except requests.exceptions.HTTPError as e:
                print(f"[odds-api] {sport_key}: {e}")
                if e.response is not None and e.response.status_code == 401:
                    # neplatný klíč NEBO došlá kvóta appky — appka appku dál
                    # nebude volat, výsledek by byl stejný appky pro každou
                    # další ligu
                    break
                continue
            except requests.exceptions.RequestException as e:
                print(f"[odds-api] {sport_key}: {e}")
                continue
            data = resp.json()
            self._cache.set(cache_key, data)
            try:
                import db as _db
                _db.cache_set(cache_key, data, ttl_seconds=self.DB_CACHE_TTL_SECONDS)
            except Exception as e:
                print(f"[odds-api] Uložení do DB cache selhalo: {e}")
            events.extend(data)
        return events

    def get_event_odds(self, sport_key: str, event_id: str, markets: str, regions: str = "eu") -> Optional[dict]:
        """
        Dotaz na KONKRÉTNÍ zápas (/events/{id}/odds) — na rozdíl od get_odds
        (jedna liga = appka dostane VŠECHNY zápasy dané ligy) appka tohle
        volá jednotlivě, protože jen tenhle endpoint appce vrátí
        "nefeaturované" trhy jako double_chance/totals_h1 (hromadný /odds
        appce na ně vrátí INVALID_MARKET, appka to ověřila živě). Dražší
        na kredity (appka to proto volá jen pro malou shortlist, viz
        _enrich_shortlist_with_extra_markets v backend_api.py), tak appka
        kešuje stejně dlouho jako get_odds (týden).
        """
        cache_key = f"odds_event:{event_id}:{markets}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            import db as _db
            db_cached = _db.cache_get(cache_key)
        except Exception:
            db_cached = None
        if db_cached is not None:
            self._cache.set(cache_key, db_cached)
            return db_cached

        try:
            resp = requests.get(
                f"{self.BASE_URL}/sports/{sport_key}/events/{event_id}/odds",
                params={"apiKey": self.api_key, "regions": regions, "markets": markets, "oddsFormat": "decimal"},
                timeout=8,
            )
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"[odds-api] event {event_id}: {e}")
            return None

        data = resp.json()
        self._cache.set(cache_key, data)
        try:
            import db as _db
            _db.cache_set(cache_key, data, ttl_seconds=self.DB_CACHE_TTL_SECONDS)
        except Exception as e:
            print(f"[odds-api] Uložení do DB cache selhalo: {e}")
        return data


# the-odds-api a API-Football appce dávají jméno stejného týmu často jinak
# napsané (diakritika, klubové zkratky, "Utd" vs "United"...) — přesná shoda
# stringů proto párovala jen ~5-10 % zápasů, i když appka reálné kurzy měla.
# Appka teď normalizuje (diakritika pryč, jen generické klubové zkratky typu
# "FC"/"AFC" pryč — NIKDY slova co odlišují kluby jako "United"/"City"/"Real",
# to by naopak dvě různé mužstva slilo v jedno) a pak hledá nejlepší fuzzy
# shodu podle podobnosti stringu. Kvůli riziku špatného spárování kurzu k
# JINÉMU zápasu appka radši nepáruje nic, než aby si nebyla dost jistá —
# vyžaduje vysoký práh podobnosti NA OBOU jménech zároveň, navíc omezuje
# hledání jen na zápasy ve stejný den (commence_time appka porovná s
# kickoff_date), a když je druhá nejlepší shoda skoro stejně dobrá jako
# první (nejednoznačnost), appku radši zahodí obě.
_TEAM_NAME_GENERIC_SUFFIXES = {
    "fc", "cf", "afc", "cfc", "sc", "ac", "sk", "fk", "bk", "if", "ff", "ifk", "ud", "cd",
    "as", "us", "ss", "ssd", "asd",
}
# Různí provideři appce dávají stejné město anglicky vs. místním jazykem
# (běžné hlavně u zápasů appkou vybraných z ne-anglických lig) — bez
# překladu appka tyhle případy fuzzy shodou nechytí (přílišné snížení
# _NAME_MATCH_THRESHOLD by naopak začalo plést jinak podobné, ale RŮZNÉ
# kluby jako "Manchester United"/"Manchester City").
_TEAM_NAME_ALIASES = {
    "praha": "prague", "munchen": "munich", "wien": "vienna", "warszawa": "warsaw",
    "moskva": "moscow", "kobenhavn": "copenhagen", "goteborg": "gothenburg",
    "roma": "rome", "torino": "turin", "milano": "milan", "napoli": "naples",
    "sevilla": "seville", "athen": "athens", "beograd": "belgrade",
    "koln": "cologne", "brasil": "brazil", "utd": "united",
}
_NAME_MATCH_THRESHOLD = 0.84
_NAME_MATCH_MARGIN = 0.05


def _normalize_team_name(name: str) -> str:
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    ascii_name = ascii_name.lower()
    ascii_name = re.sub(r"[^a-z0-9\s]", " ", ascii_name)
    words = [
        _TEAM_NAME_ALIASES.get(w, w)
        for w in ascii_name.split()
        if w not in _TEAM_NAME_GENERIC_SUFFIXES
    ]
    return " ".join(words) if words else ascii_name.strip()


def _name_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def find_matching_odds_event(events: list[dict], home_team: str, away_team: str, kickoff_date: Optional[str] = None) -> Optional[dict]:
    """
    Najde v `events` (odpověď the-odds-api) ten, co nejlíp fuzzy-odpovídá
    zadanému zápasu. Vrátí None, pokud si appka není dost jistá — viz
    komentář u konstant výše.
    """
    norm_home, norm_away = _normalize_team_name(home_team), _normalize_team_name(away_team)
    if not norm_home or not norm_away:
        return None

    scored: list[tuple[float, dict]] = []
    for event in events:
        if kickoff_date:
            commence = event.get("commence_time", "")
            if commence:
                event_date = commence[:10]
                # Tolerance ±1 den kvůli časovým pásmům/půlnoci
                try:
                    d1 = datetime.fromisoformat(kickoff_date)
                    d2 = datetime.fromisoformat(event_date)
                    if abs((d1 - d2).days) > 1:
                        continue
                except ValueError:
                    pass

        home_score = _name_similarity(norm_home, _normalize_team_name(event.get("home_team", "")))
        away_score = _name_similarity(norm_away, _normalize_team_name(event.get("away_team", "")))
        combined = min(home_score, away_score)
        if home_score >= _NAME_MATCH_THRESHOLD and away_score >= _NAME_MATCH_THRESHOLD:
            scored.append((combined, event))

    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    if len(scored) > 1 and (scored[0][0] - scored[1][0]) < _NAME_MATCH_MARGIN:
        return None  # nejednoznačné — dva zápasy skoro stejně podobné, appka radši nic nepáruje
    return scored[0][1]


def adapt_odds_api_event(event: dict) -> dict:
    """
    Z jednoho the-odds-api eventu spočítá de-vigovanou pravděpodobnost pro
    match_winner, btts a (pokud dostupný) totals trh — agregovanou napříč
    VŠEMI bookmakery v odpovědi, ne jen prvním. Appka pro každého
    bookmakera nejdřív spočítá jeho vlastní de-vigovanou pravděpodobnost,
    pak je zprůměruje — to je skutečný "market consensus", ne jen názor
    jednoho konkrétního bookmakera. Cena pro staking (favorite_win_market_odds,
    over_odds, btts_yes_odds) je medián napříč bookmakery. Párování na
    zápas z jiného providera je tu jen přes přesnou shodu jména týmu/
    hráče (event["home_team"]/["away_team"]) — v produkci by chtělo
    robustnější fuzzy matching, jména se mezi providery často liší.
    """
    home_name, away_name = event["home_team"], event["away_team"]
    result = {
        "home_team": home_name, "away_team": away_name,
        "favorite_win_market_odds": None,
        "market_implied_probabilities": {},
        "over_threshold": None, "over_odds": None, "over_probability": None,
        "under_odds": None, "under_probability": None,
        "btts_yes_odds": None,
        "bookmaker_count": len(event.get("bookmakers", [])),
    }
    bookmakers = event.get("bookmakers", [])
    if not bookmakers:
        return result

    home_probs, away_probs, home_prices = [], [], []
    btts_probs, btts_prices = [], []
    totals_by_threshold: dict[float, list[tuple[float, float, float, float]]] = {}  # threshold -> [(cena_over, p_over, cena_under, p_under), ...]

    for bm in bookmakers:
        markets = bm.get("markets", [])

        h2h = next((m for m in markets if m["key"] == "h2h"), None)
        if h2h:
            outcomes = [(o["name"], o["price"]) for o in h2h["outcomes"]]
            probs = devig_market(outcomes)
            if home_name in probs:
                home_probs.append(probs[home_name])
                home_prices.append(next(o["price"] for o in h2h["outcomes"] if o["name"] == home_name))
            if away_name in probs:
                away_probs.append(probs[away_name])

        btts = next((m for m in markets if m["key"] == "btts"), None)
        if btts:
            yes_o = next((o for o in btts["outcomes"] if o["name"] == "Yes"), None)
            no_o = next((o for o in btts["outcomes"] if o["name"] == "No"), None)
            if yes_o and no_o:
                p_yes, _ = devig_two_way(yes_o["price"], no_o["price"])
                btts_probs.append(p_yes)
                btts_prices.append(yes_o["price"])

        totals = next((m for m in markets if m["key"] == "totals"), None)
        if totals:
            over_o = next((o for o in totals["outcomes"] if o["name"] == "Over"), None)
            under_o = next((o for o in totals["outcomes"] if o["name"] == "Under"), None)
            if over_o and under_o:
                p_over, p_under = devig_two_way(over_o["price"], under_o["price"])
                totals_by_threshold.setdefault(over_o["point"], []).append(
                    (over_o["price"], p_over, under_o["price"], p_under)
                )

    if home_probs:
        result["market_implied_probabilities"]["match_winner:home"] = sum(home_probs) / len(home_probs)
        result["favorite_win_market_odds"] = _median(home_prices)
    if away_probs:
        result["market_implied_probabilities"]["match_winner:away"] = sum(away_probs) / len(away_probs)

    if btts_probs:
        result["market_implied_probabilities"]["btts:yes"] = sum(btts_probs) / len(btts_probs)
        result["btts_yes_odds"] = _median(btts_prices)

    if totals_by_threshold:
        # appka bere hranici s nejvíc pozorováními napříč bookmakery
        # (typicky 2.5 góly — ta bývá nabízená skoro všude)
        threshold = max(totals_by_threshold, key=lambda t: len(totals_by_threshold[t]))
        prices_and_probs = totals_by_threshold[threshold]
        result["over_threshold"] = threshold
        result["over_odds"] = _median([p for p, _, _, _ in prices_and_probs])
        result["over_probability"] = sum(p for _, p, _, _ in prices_and_probs) / len(prices_and_probs)
        result["under_odds"] = _median([p for _, _, p, _ in prices_and_probs])
        result["under_probability"] = sum(p for _, _, _, p in prices_and_probs) / len(prices_and_probs)

    return result


def adapt_odds_api_extra_markets(event: dict) -> dict:
    """
    Dvojtip (double_chance) a poločasové góly (totals_h1) — na rozdíl od
    adapt_odds_api_event výše appka tohle NEDOSTÁVÁ z hromadného /odds
    dotazu (ten appce na tyhle trhy vrací INVALID_MARKET), appka je tahá
    zvlášť přes dotaz na konkrétní zápas (/events/{id}/odds), a jen pro
    malou shortlist zápasů (viz _enrich_shortlist_with_extra_markets v
    backend_api.py — cena za zápas je vyšší, appka to nedělá plošně).

    Dvojtip appka NEDE-VIGUJE (na rozdíl od h2h/totals výše) — 1X/X2/12
    se navzájem překrývají (každý výsledek počítá do dvou z nich), takže
    devig_market (počítá s třemi VZÁJEMNĚ SE VYLUČUJÍCÍMI výsledky) by
    dal špatné číslo. Appka místo toho bere holé 1/kurz (medián napříč
    bookmakery) — obsahuje marži bookmakera, tedy mírně PODHODNOCuje
    skutečnou pravděpodobnost, což je konzervativní (bezpečná) strana
    chyby, ne riziková.
    """
    result = {
        "double_chance_odds": {},
        "market_implied_probabilities": {},
        "ht_over_threshold": None, "ht_over_odds": None, "ht_over_probability": None,
        "ht_under_odds": None, "ht_under_probability": None,
    }
    bookmakers = event.get("bookmakers", [])
    if not bookmakers:
        return result

    dc_prices: dict[str, list[float]] = {"1X": [], "X2": [], "12": []}
    dc_name_map = {
        f"{event.get('home_team')} or Draw": "1X",
        f"{event.get('away_team')} or Draw": "X2",
        f"{event.get('home_team')} or {event.get('away_team')}": "12",
    }
    ht_totals_by_threshold: dict[float, list[tuple[float, float, float, float]]] = {}

    for bm in bookmakers:
        markets = bm.get("markets", [])

        dc = next((m for m in markets if m["key"] == "double_chance"), None)
        if dc:
            for o in dc["outcomes"]:
                key = dc_name_map.get(o["name"])
                if key:
                    dc_prices[key].append(o["price"])

        ht_totals = next((m for m in markets if m["key"] == "totals_h1"), None)
        if ht_totals:
            over_o = next((o for o in ht_totals["outcomes"] if o["name"] == "Over"), None)
            under_o = next((o for o in ht_totals["outcomes"] if o["name"] == "Under"), None)
            if over_o and under_o:
                p_over, p_under = devig_two_way(over_o["price"], under_o["price"])
                ht_totals_by_threshold.setdefault(over_o["point"], []).append(
                    (over_o["price"], p_over, under_o["price"], p_under)
                )

    for key, prices in dc_prices.items():
        if prices:
            odds = _median(prices)
            result["double_chance_odds"][key] = odds
            result["market_implied_probabilities"][f"double_chance:{key}"] = 1.0 / odds

    if ht_totals_by_threshold:
        threshold = max(ht_totals_by_threshold, key=lambda t: len(ht_totals_by_threshold[t]))
        prices_and_probs = ht_totals_by_threshold[threshold]
        result["ht_over_threshold"] = threshold
        result["ht_over_odds"] = _median([p for p, _, _, _ in prices_and_probs])
        result["ht_over_probability"] = sum(p for _, p, _, _ in prices_and_probs) / len(prices_and_probs)
        result["ht_under_odds"] = _median([p for _, _, p, _ in prices_and_probs])
        result["ht_under_probability"] = sum(p for _, _, _, p in prices_and_probs) / len(prices_and_probs)
        result["market_implied_probabilities"][f"ht_over_goals:over_{threshold}"] = result["ht_over_probability"]
        result["market_implied_probabilities"][f"ht_over_goals:under_{threshold}"] = result["ht_under_probability"]

    return result


# =======================================================================
# SPORTMONKS — třetí zdroj kurzů, doplňkový k API-Football + the-odds-api.
# Cíl: pokrýt zápasy, kde ani jeden z nich nemá reálnou tržní cenu
# (česká Fortuna liga, Peru Liga 1, mimosezónní evropské kvalifikace —
# viz komentář u SPORT_KEYS výše). Bez SportMonks appka pro tyhle zápasy
# dřív musela cenu VYMÝŠLET z vlastního modelu (favorite_odds fallback v
# normalize_to_match_input), což appka živě potvrdila jako reálný problém
# (rozdíl 2-50 % proti skutečným Tipsport cenám u zápasu Vlašim/PAOK/Alianza).
#
# POZOR — endpoint cesty a JSON tvar odds objektů NEJSOU živě ověřené.
# SportMonks dokumentace (docs.sportmonks.com/v3) appce při přípravě téhle
# třídy vracela jen částečný obsah (404/zkrácené stránky), takže appka
# cesty a názvy trhů níže sestavila z jejich veřejně známého v3 schématu
# (base URL, auth přes api_token/Authorization appka OVĚŘILA), ne z
# kompletní specifikace. PRVNÍ věc po získání tokenu: zavolat get_fixtures_by_date
# na pár dní dopředu a projít si skutečnou strukturu odpovědi (marketů i
# odds), než se tahle třída zapojí do generování — market_id/label názvy
# (FULLTIME_RESULT_MARKET_ID atd.) níže se podle toho pravděpodobně budou
# muset upravit.
# Dokumentace: https://docs.sportmonks.com/v3
# =======================================================================
SPORTMONKS_BASE_URL = "https://api.sportmonks.com/api/v3"

# Odhad podle veřejně zdokumentovaného v3 schématu (market "name"/"label"
# stringy) — appka je zatím NEPOUŽÍVÁ nikde jinde v pipeline, jen v týhle
# třídě, ať je snadné je na jednom místě opravit po prvním živém testu.
SPORTMONKS_MARKET_FULLTIME_RESULT = "Fulltime Result"   # výsledky: "Home", "Draw", "Away"
SPORTMONKS_MARKET_OVER_UNDER = "Goals Over/Under"        # výsledky: "Over 2.5", "Under 2.5"... (label obsahuje total)
SPORTMONKS_MARKET_DOUBLE_CHANCE = "Double Chance"        # výsledky: "Home/Draw", "Draw/Away", "Home/Away"


class SportMonksProvider:
    def __init__(self, api_key: Optional[str] = None, cache_ttl_seconds: int = 300):
        self.api_key = api_key or os.environ.get("SPORTMONKS_KEY", "")
        if not self.api_key:
            raise RuntimeError("Chybí SPORTMONKS_KEY (proměnná prostředí).")
        self._cache = InMemoryCache(ttl_seconds=cache_ttl_seconds)

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        query = {"api_token": self.api_key, **(params or {})}
        resp = requests.get(f"{SPORTMONKS_BASE_URL}{path}", params=query, timeout=8)
        resp.raise_for_status()
        payload = resp.json()
        if "message" in payload and "data" not in payload:
            # SportMonks chybové odpovědi appka zatím jen viděla popsané
            # v dokumentaci (ne živě) — tvar "message" bez "data" appka
            # bere jako chybu, dokud test s reálným tokenem neukáže jinak.
            raise RuntimeError(f"SportMonks vrátilo chybu: {payload.get('message')}")
        return payload

    def get_fixtures_by_date(self, day: date, include: str = "odds;participants") -> list[dict]:
        """
        Zápasy pro jeden konkrétní den, včetně kurzů a týmů (participants).
        Appka je kešuje 30 min (stejně jako API-Football upcoming) — kurzy
        se v průběhu dne mění, appka nechce zbytečně stará čísla.
        """
        cache_key = f"sm_fixtures:{day.isoformat()}:{include}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            import db as _db
            db_cached = _db.cache_get(cache_key)
            if db_cached is not None:
                self._cache.set(cache_key, db_cached)
                return db_cached
        except Exception:
            pass

        payload = self._get(f"/football/fixtures/date/{day.isoformat()}", {"include": include})
        fixtures = payload.get("data", [])
        self._cache.set(cache_key, fixtures)
        try:
            import db as _db
            _db.cache_set(cache_key, fixtures, ttl_seconds=30 * 60)
        except Exception:
            pass
        return fixtures

    def find_matching_fixture(self, day: date, home_team: str, away_team: str) -> Optional[dict]:
        """
        Stejná fuzzy-matching logika jako find_matching_odds_event
        (the-odds-api) — SportMonks a API-Football taky nedávají jména
        týmů stejně napsaná. Appka porovnává jen zápasy ze stejného dne.
        """
        fixtures = self.get_fixtures_by_date(day)
        norm_home, norm_away = _normalize_team_name(home_team), _normalize_team_name(away_team)
        if not norm_home or not norm_away:
            return None

        scored: list[tuple[float, dict]] = []
        for fx in fixtures:
            participants = fx.get("participants", [])
            fx_home = next((p.get("name", "") for p in participants if p.get("meta", {}).get("location") == "home"), "")
            fx_away = next((p.get("name", "") for p in participants if p.get("meta", {}).get("location") == "away"), "")
            home_score = _name_similarity(norm_home, _normalize_team_name(fx_home))
            away_score = _name_similarity(norm_away, _normalize_team_name(fx_away))
            if home_score >= _NAME_MATCH_THRESHOLD and away_score >= _NAME_MATCH_THRESHOLD:
                scored.append((min(home_score, away_score), fx))

        if not scored:
            return None
        scored.sort(key=lambda x: x[0], reverse=True)
        if len(scored) > 1 and (scored[0][0] - scored[1][0]) < _NAME_MATCH_MARGIN:
            return None
        return scored[0][1]


def adapt_sportmonks_odds(fixture: dict) -> dict:
    """
    Z jednoho SportMonks fixture (s include=odds) spočítá stejný tvar
    dat jako adapt_odds_api_event (the-odds-api) — favorite_win_market_odds,
    over/under kurzy, market_implied_probabilities — appka je pak
    kombinuje stejnou logikou, ať appka nemusí duplikovat kód nahoru
    v _enrich_with_market_odds.

    NEOVĚŘENO ŽIVĚ — market/label názvy (SPORTMONKS_MARKET_* konstanty
    výše) jsou z veřejné dokumentace, ne z reálné odpovědi. Appka to
    napravuje hned po prvním testu s reálným tokenem.
    """
    result = {
        "favorite_win_market_odds": None,
        "market_implied_probabilities": {},
        "over_threshold": None, "over_odds": None, "over_probability": None,
        "under_odds": None, "under_probability": None,
        "bookmaker_count": 0,
    }
    odds_entries = fixture.get("odds", [])
    if not odds_entries:
        return result

    bookmaker_ids = {o.get("bookmaker_id") for o in odds_entries if o.get("bookmaker_id")}
    result["bookmaker_count"] = len(bookmaker_ids)

    home_prices = [o["value"] for o in odds_entries if o.get("market_description") == SPORTMONKS_MARKET_FULLTIME_RESULT and o.get("label") == "Home"]
    away_prices = [o["value"] for o in odds_entries if o.get("market_description") == SPORTMONKS_MARKET_FULLTIME_RESULT and o.get("label") == "Away"]
    draw_prices = [o["value"] for o in odds_entries if o.get("market_description") == SPORTMONKS_MARKET_FULLTIME_RESULT and o.get("label") == "Draw"]

    if home_prices and away_prices and draw_prices:
        outcomes = [("home", float(_median(home_prices))), ("away", float(_median(away_prices))), ("draw", float(_median(draw_prices)))]
        probs = devig_market(outcomes)
        if "home" in probs:
            result["market_implied_probabilities"]["match_winner:home"] = probs["home"]
            result["favorite_win_market_odds"] = float(_median(home_prices))
        if "away" in probs:
            result["market_implied_probabilities"]["match_winner:away"] = probs["away"]

    totals_by_threshold: dict[float, list[tuple[float, float]]] = {}
    for o in odds_entries:
        if o.get("market_description") != SPORTMONKS_MARKET_OVER_UNDER:
            continue
        label = o.get("label", "")
        total = o.get("total")
        if total is None or not label:
            continue
        totals_by_threshold.setdefault(float(total), {"over": [], "under": []})
        if label.lower() == "over":
            totals_by_threshold[float(total)]["over"].append(float(o["value"]))
        elif label.lower() == "under":
            totals_by_threshold[float(total)]["under"].append(float(o["value"]))

    for threshold, sides in totals_by_threshold.items():
        if sides["over"] and sides["under"]:
            over_price, under_price = _median(sides["over"]), _median(sides["under"])
            p_over, p_under = devig_two_way(over_price, under_price)
            totals_by_threshold[threshold] = (over_price, p_over, under_price, p_under)
        else:
            totals_by_threshold[threshold] = None
    valid_thresholds = {t: v for t, v in totals_by_threshold.items() if v is not None}
    if valid_thresholds:
        threshold = max(valid_thresholds, key=lambda t: 1)  # appka nemá počet pozorování jako u the-odds-api — bere první platnou
        over_price, p_over, under_price, p_under = valid_thresholds[threshold]
        result["over_threshold"] = threshold
        result["over_odds"] = over_price
        result["over_probability"] = p_over
        result["under_odds"] = under_price
        result["under_probability"] = p_under

    return result


# =======================================================================
# ODDSPAPI — třetí zdroj kurzů, AKTIVNÍ (na rozdíl od SportMonks výše,
# co zůstává jen připravený, nezapojený kód). Appka ho živě ověřila
# 2026-08-05 na přesně těch ligách, co API-Football ani the-odds-api
# nepokrývají (česká Fortuna liga = TIPSPORT_LEAGUE_IDS 345, Peru Liga 1 =
# 281) — 1X2, dvojtip i poločasové góly appka reálně dostala, přes 100
# bookmakerů na zápas (mj. Pinnacle, bet365). Karta/platba appka
# nepotřebovala, free tier (250 requestů/měsíc, bez expirace) appka
# ověřila zdarma.
#
# Market/outcome ID NÍŽE appka získala živě přes GET /markets (ne z
# dokumentace — ta appce při přípravě SportMonks výše nešla stáhnout
# kompletní, tady appka radši rovnou sáhla po reálném API). Jeden
# /odds dotaz na fixtureId appce vrátí VŠECHNY trhy a VŠECHNY bookmakery
# najednou (na rozdíl od the-odds-api, kde appka potřebuje dva různé
# requesty — hromadný pro 1X2/totals a per-event pro dvojtip/poločas).
#
# ROZPOČET: appka má jen 250 requestů/měsíc zdarma, /odds odpověď je
# navíc velká (~8 MB na zápas) — appka ho proto volá JEN pro zápasy, co
# ani API-Football ani the-odds-api nenapárovaly na žádný reálný kurz
# (viz _enrich_with_oddspapi v backend_api.py), a jen do malého stropu
# na jedno generování, stejně jako u MAX_EXTRA_MARKET_SHORTLIST.
# Dokumentace: https://oddspapi.io/docs
# =======================================================================
class OddsPapiProvider:
    BASE_URL = "https://api.oddspapi.io/v4"
    SPORT_ID_FOOTBALL = 10

    # marketId -> (over_outcome_id, under_outcome_id). Appka ověřila živě:
    # marketId appky u těchhle trhů VŽDY odpovídá prvnímu outcomeId, druhý
    # outcome (Under) je o 1 vyšší — appka to nechává jako explicitní
    # dict (ne odvozené +1 v kódu), ať je to čitelné a appka to nemusí
    # znovu dohledávat, kdyby se to u jiného trhu nepotvrdilo.
    FT_RESULT_MARKET_ID = 101
    FT_RESULT_OUTCOME_IDS = {"home": 101, "draw": 102, "away": 103}
    DOUBLE_CHANCE_MARKET_ID = 101902
    DOUBLE_CHANCE_OUTCOME_MAP = {101902: "1X", 101903: "12", 101904: "X2"}
    FT_TOTALS_THRESHOLDS: dict[float, tuple[int, int]] = {
        0.5: (106, 107), 1.5: (108, 109), 2.5: (1010, 1011), 3.5: (1012, 1013), 4.5: (1014, 1015),
    }
    HT_TOTALS_THRESHOLDS: dict[float, tuple[int, int]] = {
        0.5: (10256, 10257), 1.5: (10258, 10259), 2.5: (10260, 10261), 3.5: (10262, 10263),
    }

    def __init__(self, api_key: Optional[str] = None, cache_ttl_seconds: int = 300):
        self.api_key = api_key or os.environ.get("ODDSPAPI_KEY", "")
        if not self.api_key:
            raise RuntimeError("Chybí ODDSPAPI_KEY (proměnná prostředí).")
        self._cache = InMemoryCache(ttl_seconds=cache_ttl_seconds)

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        query = {"apiKey": self.api_key, **(params or {})}
        resp = requests.get(f"{self.BASE_URL}{path}", params=query, timeout=20)
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and "error" in payload:
            raise RuntimeError(f"OddsPapi vrátilo chybu: {payload['error']}")
        return payload

    def get_tournaments(self, sport_id: int = SPORT_ID_FOOTBALL) -> list[dict]:
        """
        Seznam soutěží appka mění jen výjimečně (nový ročník, přejmenování)
        — dlouhá TTL (appka ho kešuje týden, stejně jako kurzy u ostatních
        providerů), ať appka zbytečně neplýtvá měsíční kvótou 250 requestů.
        """
        cache_key = f"op_tournaments:{sport_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            import db as _db
            db_cached = _db.cache_get(cache_key)
            if db_cached is not None:
                self._cache.set(cache_key, db_cached)
                return db_cached
        except Exception:
            pass
        tournaments = self._get("/tournaments", {"sportId": sport_id})
        self._cache.set(cache_key, tournaments)
        try:
            import db as _db
            _db.cache_set(cache_key, tournaments, ttl_seconds=7 * 24 * 3600)
        except Exception:
            pass
        return tournaments

    def find_tournament_candidates(self, country_name: str, league_name: str) -> list[tuple[float, dict]]:
        """
        Appka to zkusila přes fuzzy shodu (categoryName/tournamentName), ale
        appka živě ověřila, že to NENÍ spolehlivé — API-Football appce dává
        "Chance Liga"/"Czech Republic", OddsPapi appce dává "1. Liga"/
        "Czechia" (podobnost 0.57-0.59, hluboko pod _NAME_MATCH_THRESHOLD
        0.84 appka používá pro jména týmů). Appka proto v produkci
        (_enrich_with_oddspapi v backend_api.py) používá ODDSPAPI_TOURNAMENT_IDS
        (explicitní mapování podle league_id, stejný princip jako
        TIPSPORT_LEAGUE_IDS/SPORT_KEYS výše) — tahle metoda zůstává jen
        jako pomocný diagnostický nástroj pro RUČNÍ dohledání dalších ID
        (appka radši vrátí víc kandidátů s nízkým prahem, než aby appka
        automaticky spoléhala na nejistou shodu bez lidské kontroly).
        """
        tournaments = self.get_tournaments()
        norm_country = _normalize_team_name(country_name)
        norm_league = _normalize_team_name(league_name)
        scored: list[tuple[float, dict]] = []
        for t in tournaments:
            league_score = _name_similarity(norm_league, _normalize_team_name(t.get("tournamentName", "")))
            country_score = _name_similarity(norm_country, _normalize_team_name(t.get("categoryName", "")))
            scored.append((league_score * 0.7 + country_score * 0.3, t))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:5]  # appka vrací TOP 5 kandidátů k ruční kontrole, ne jedno "jisté" ID


    def get_fixtures(self, tournament_id: int) -> list[dict]:
        cache_key = f"op_fixtures:{tournament_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            import db as _db
            db_cached = _db.cache_get(cache_key)
            if db_cached is not None:
                self._cache.set(cache_key, db_cached)
                return db_cached
        except Exception:
            pass
        fixtures = self._get("/fixtures", {"tournamentId": tournament_id})
        self._cache.set(cache_key, fixtures)
        try:
            import db as _db
            _db.cache_set(cache_key, fixtures, ttl_seconds=30 * 60)
        except Exception:
            pass
        return fixtures

    def find_matching_fixture(self, tournament_id: int, home_team: str, away_team: str, kickoff_date: Optional[str] = None) -> Optional[dict]:
        fixtures = self.get_fixtures(tournament_id)
        norm_home, norm_away = _normalize_team_name(home_team), _normalize_team_name(away_team)
        if not norm_home or not norm_away:
            return None

        scored: list[tuple[float, dict]] = []
        for fx in fixtures:
            if not fx.get("hasOdds"):
                continue
            if kickoff_date:
                start = fx.get("startTime", "")
                if start and start[:10] != kickoff_date:
                    continue
            home_score = _name_similarity(norm_home, _normalize_team_name(fx.get("participant1Name", "")))
            away_score = _name_similarity(norm_away, _normalize_team_name(fx.get("participant2Name", "")))
            if home_score >= _NAME_MATCH_THRESHOLD and away_score >= _NAME_MATCH_THRESHOLD:
                scored.append((min(home_score, away_score), fx))

        if not scored:
            return None
        scored.sort(key=lambda x: x[0], reverse=True)
        if len(scored) > 1 and (scored[0][0] - scored[1][0]) < _NAME_MATCH_MARGIN:
            return None
        return scored[0][1]

    def get_odds(self, fixture_id: str) -> Optional[dict]:
        """
        Appka tenhle request kešuje týden (stejně jako the-odds-api) —
        odpověď je velká (~8 MB) a appka má jen 250/měsíc, takže appka
        nechce stejný zápas stahovat znovu při každém dalším generování
        ten samý den.
        """
        cache_key = f"op_odds:{fixture_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            import db as _db
            db_cached = _db.cache_get(cache_key)
            if db_cached is not None:
                self._cache.set(cache_key, db_cached)
                return db_cached
        except Exception:
            pass
        data = self._get("/odds", {"fixtureId": fixture_id})
        self._cache.set(cache_key, data)
        try:
            import db as _db
            _db.cache_set(cache_key, data, ttl_seconds=7 * 24 * 3600)
        except Exception as e:
            print(f"[oddspapi] Uložení do DB cache selhalo: {e}")
        return data


# API-Football league_id -> OddsPapi tournamentId — explicitní mapování,
# appka ho živě ověřila jen pro tyhle dvě soutěže (2026-08-05). Stejná
# mezera appka zjistila u the-odds-api (viz komentář u SPORT_KEYS výše —
# Rumunsko, Česko, Turecko, Chorvatsko, Srbsko, Maďarsko, Slovensko,
# Kypr, Izrael, Uruguay, Kolumbie, Peru, Japonsko, Austrálie, Saúdská
# Arábie, mezinárodní kvalifikace appce v the-odds-api chybí). Doplň
# další ID, jak appka ověří další chybějící ligy — tournamentId najdeš
# přes OddsPapiProvider.find_tournament_candidates(country, league) nebo
# GET /admin/test-oddspapi.
ODDSPAPI_TOURNAMENT_IDS: dict[int, int] = {
    345: 172,  # Chance Liga (ČR) -> OddsPapi "1. Liga" (Czechia)
    281: 406,  # Primera División (Peru) -> OddsPapi "Liga 1" (Peru)
}


def adapt_oddspapi_odds(odds_response: dict) -> dict:
    """
    Z JEDNÉ OddsPapi odpovědi (/odds?fixtureId=...) spočítá stejný tvar
    dat jako adapt_odds_api_event + adapt_odds_api_extra_markets
    dohromady (1X2, dvojtip, poločasové góly) — na rozdíl od the-odds-api
    appka tu nepotřebuje dva různé requesty, jeden appce dá VŠECHNY trhy
    pro daný zápas najednou (appka to živě ověřila, viz komentář u
    OddsPapiProvider výše).
    """
    result = {
        "favorite_win_market_odds": None,
        "market_implied_probabilities": {},
        "over_threshold": None, "over_odds": None, "over_probability": None,
        "under_odds": None, "under_probability": None,
        "double_chance_odds": {},
        "ht_over_threshold": None, "ht_over_odds": None, "ht_over_probability": None,
        "ht_under_odds": None, "ht_under_probability": None,
        "bookmaker_count": 0,
    }
    bookmaker_odds = odds_response.get("bookmakerOdds", {}) if odds_response else {}
    if not bookmaker_odds:
        return result
    result["bookmaker_count"] = len(bookmaker_odds)

    def _price(bm_markets: dict, market_id: int, outcome_id: int) -> Optional[float]:
        outcome = bm_markets.get(str(market_id), {}).get("outcomes", {}).get(str(outcome_id), {})
        price = outcome.get("players", {}).get("0", {}).get("price")
        return float(price) if price else None

    home_probs, home_prices, away_probs = [], [], []
    dc_prices: dict[str, list[float]] = {"1X": [], "12": [], "X2": []}
    ft_totals_by_threshold: dict[float, list[tuple[float, float, float, float]]] = {}
    ht_totals_by_threshold: dict[float, list[tuple[float, float, float, float]]] = {}

    for bm in bookmaker_odds.values():
        markets = bm.get("markets", {})

        home_p = _price(markets, OddsPapiProvider.FT_RESULT_MARKET_ID, OddsPapiProvider.FT_RESULT_OUTCOME_IDS["home"])
        draw_p = _price(markets, OddsPapiProvider.FT_RESULT_MARKET_ID, OddsPapiProvider.FT_RESULT_OUTCOME_IDS["draw"])
        away_p = _price(markets, OddsPapiProvider.FT_RESULT_MARKET_ID, OddsPapiProvider.FT_RESULT_OUTCOME_IDS["away"])
        if home_p and draw_p and away_p:
            probs = devig_market([("home", home_p), ("draw", draw_p), ("away", away_p)])
            if "home" in probs:
                home_probs.append(probs["home"])
                home_prices.append(home_p)
            if "away" in probs:
                away_probs.append(probs["away"])

        for outcome_id, key in OddsPapiProvider.DOUBLE_CHANCE_OUTCOME_MAP.items():
            price = _price(markets, OddsPapiProvider.DOUBLE_CHANCE_MARKET_ID, outcome_id)
            if price:
                dc_prices[key].append(price)

        # Appka živě ověřila (2026-08-05): marketId u Over/Under trhů appce
        # odpovídá VŽDY jen tomu Over outcomu (over_id) — market s tímhle
        # ID appce vrací OBĚ strany (Over i Under) jako dva outcomes uvnitř
        # sebe, ne dva samostatné markety. under_id appka proto použije
        # jen jako outcomeId, ne jako druhý market_id (appka to napoprvé
        # spletla, appka to opravila po testu na reálné odpovědi — bez
        # tyhle opravy appce vždycky vyšlo 0 nalezených prahů).
        for threshold, (over_id, under_id) in OddsPapiProvider.FT_TOTALS_THRESHOLDS.items():
            over_p = _price(markets, over_id, over_id)
            under_p = _price(markets, over_id, under_id)
            if over_p and under_p:
                p_over, p_under = devig_two_way(over_p, under_p)
                ft_totals_by_threshold.setdefault(threshold, []).append((over_p, p_over, under_p, p_under))

        for threshold, (over_id, under_id) in OddsPapiProvider.HT_TOTALS_THRESHOLDS.items():
            over_p = _price(markets, over_id, over_id)
            under_p = _price(markets, over_id, under_id)
            if over_p and under_p:
                p_over, p_under = devig_two_way(over_p, under_p)
                ht_totals_by_threshold.setdefault(threshold, []).append((over_p, p_over, under_p, p_under))

    if home_probs:
        result["market_implied_probabilities"]["match_winner:home"] = sum(home_probs) / len(home_probs)
        result["favorite_win_market_odds"] = _median(home_prices)
    if away_probs:
        result["market_implied_probabilities"]["match_winner:away"] = sum(away_probs) / len(away_probs)

    for key, prices in dc_prices.items():
        if prices:
            odds = _median(prices)
            result["double_chance_odds"][key] = odds
            result["market_implied_probabilities"][f"double_chance:{key}"] = 1.0 / odds

    if ft_totals_by_threshold:
        threshold = max(ft_totals_by_threshold, key=lambda t: len(ft_totals_by_threshold[t]))
        pp = ft_totals_by_threshold[threshold]
        result["over_threshold"] = threshold
        result["over_odds"] = _median([p for p, _, _, _ in pp])
        result["over_probability"] = sum(p for _, p, _, _ in pp) / len(pp)
        result["under_odds"] = _median([p for _, _, p, _ in pp])
        result["under_probability"] = sum(p for _, _, _, p in pp) / len(pp)

    if ht_totals_by_threshold:
        threshold = max(ht_totals_by_threshold, key=lambda t: len(ht_totals_by_threshold[t]))
        pp = ht_totals_by_threshold[threshold]
        result["ht_over_threshold"] = threshold
        result["ht_over_odds"] = _median([p for p, _, _, _ in pp])
        result["ht_over_probability"] = sum(p for _, p, _, _ in pp) / len(pp)
        result["ht_under_odds"] = _median([p for _, _, p, _ in pp])
        result["ht_under_probability"] = sum(p for _, _, _, p in pp) / len(pp)
        result["market_implied_probabilities"][f"ht_over_goals:over_{threshold}"] = result["ht_over_probability"]
        result["market_implied_probabilities"][f"ht_over_goals:under_{threshold}"] = result["ht_under_probability"]

    return result


# =======================================================================
# API-FOOTBALL — přímo přes api-sports.io (NE RapidAPI). Stejný klíč
# (APISPORTS_KEY) jako u Basketball/Hockey výše — jedna registrace na
# dashboard.api-sports.io zdarma odemkne i fotbal.
# Dokumentace: https://www.api-football.com/documentation-v3
# =======================================================================
import requests
from datetime import date, datetime, timedelta

API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"

# API-Football vyžaduje u /fixtures vždy alespoň jeden filtr (datum, liga+sezóna,
# tým...). Bez konkrétní ligy stahujeme den po dni přes 'date' parametr — funguje,
# ale je to mnohem dražší na počet requestů (a vypadá to automatickým systémům
# podezřele jako scraping). Proto je výchozí seznam vyplněný hlavními ligami —
# klidně uprav podle toho, co chceš sledovat (ID najdeš přes endpoint /leagues).
# Appka stahuje VŠECHNY zápasy daného dne přes všechny soutěže (ne jen
# předem vybraný seznam lig) — viz get_upcoming_matches. MAX_FIXTURES_PER_REQUEST
# je tvrdá pojistka, ať appka při dni s desítkami soutěží nesežere celou
# denní kvótu API na jediné generování (appka na 1 zápas potřebuje
# v praxi cca 10-12 dalších volání — statistiky, kurzy, forma, zranění,
# tabulka — víc, než se na první pohled zdá).
#
# Appka teď tyhle požadavky dělá SOUBĚŽNĚ (víc vláken najednou, viz
# FIXTURE_ENRICHMENT_WORKERS v backend_api.py), ne sekvenčně — díky tomu
# appka zvládne víc zápasů v rozumném čase bez rizika timeoutu na
# straně Render/Cloudflare. ~11 volání na zápas (statistiky, kurzy,
# forma, zranění, tabulka...).
#
# Po přechodu na Mega plán (150 000 req/den, viz _api_football_rate_limiter)
# appka zvedla limit ze 150 na 400: 400 × ~11 ≈ 4400 požadavků na jedno
# "studené" generování (bez cache) — appka jich denní kvótou snese cca
# 30+, i bez počítání s tím, že cache týmových statistik (24 h) a
# seznamu zápasů (30 min) dělá DALŠÍ generování ten samý den výrazně
# levnější. Předtím appka na Pro plánu (7 500/den, 150 zápasů) snesla jen
# 4-5 takových za den.
MAX_FIXTURES_PER_REQUEST = 400

# Ligy dostupné na Tipsport.cz — appka filtruje jen zápasy z těchto soutěží.
# Tipsport pokrývá přes 70 fotbalových soutěží z celého světa.
# ID jsou z API-Football (/leagues endpoint).
TIPSPORT_LEAGUE_IDS: set[int] = {
    # Anglie
    39,   # Premier League
    40,   # Championship
    41,   # League One
    42,   # League Two
    45,   # FA Cup
    48,   # EFL Cup
    # Německo
    78,   # Bundesliga
    79,   # 2. Bundesliga
    80,   # 3. Liga
    81,   # DFB Pokal
    # Itálie
    135,  # Serie A
    136,  # Serie B
    137,  # Coppa Italia
    # Španělsko
    140,  # La Liga
    141,  # La Liga 2
    143,  # Copa del Rey
    # Francie
    61,   # Ligue 1
    62,   # Ligue 2
    66,   # Coupe de France
    # Holandsko
    88,   # Eredivisie
    89,   # Eerste Divisie
    # Portugalsko
    94,   # Primeira Liga
    95,   # Segunda Liga
    # Belgie
    144,  # Jupiler Pro League
    # Turecko
    203,  # Süper Lig
    204,  # 1. Lig
    # Skotsko
    179,  # Scottish Premiership
    # Švýcarsko
    207,  # Swiss Super League — appka tu dřív omylem měla ID 169 (to je
          # ve skutečnosti Čína, viz níže), takže reálná švýcarská liga
          # appce celou dobu propadala sítem, zatímco čínská si ID 169
          # "půjčila" a filtrem procházela
    # Rakousko — appka tu dřív neměla VŮBEC nic, přestože appka ověřila
    # (2026-08-05), že rakouská Bundesliga se na Tipsportu reálně sází a
    # appka na ni tam má dokonce živé přenosy. ID appka ověřila přímo přes
    # /leagues (appka je tu nehádá, viz historie chyb u Švýcarska/Číny výš).
    218,  # Bundesliga
    219,  # 2. Liga
    # Bulharsko
    172,  # First League
    # Slovinsko
    373,  # 1. SNL
    # Irsko
    357,  # Premier Division
    # Island
    164,  # Úrvalsdeild
    # Švédsko
    113,  # Allsvenskan
    114,  # Superettan
    # Norsko
    103,  # Eliteserien
    104,  # 1. divisjon
    # Dánsko
    119,  # Superliga
    # Finsko
    244,  # Veikkausliiga
    # Řecko
    197,  # Super League
    # Rusko
    235,  # Premier League
    # Polsko
    106,  # Ekstraklasa
    # Maďarsko
    271,  # OTP Bank Liga
    # Rumunsko
    283,  # Liga 1
    # Srbsko
    286,  # Super Liga
    # Chorvatsko
    210,  # HNL
    # Slovensko
    332,  # Super Liga
    # Česko
    345,  # Chance Liga
    346,  # Chance Národní Liga
    # Izrael — appka tu dřív měla jen 384 jako "Premier League", ale 384 je
    # ve skutečnosti Státní pohár. Skutečná nejvyšší liga (Ligat Ha'al) má
    # ID 383 — přidáno, 384 ponechána (pohár sám o sobě je běžná sázková
    # nabídka, viz FA Cup/DFB Pokal/Coppa Italia výš).
    383,  # Ligat Ha'al (Premier League)
    384,  # State Cup
    # Kypr
    318,  # First Division — appka tu dřív omylem měla ID 262 (to je Liga MX,
          # viz Mexiko níže), takže appka fakticky filtrovala jen na Liga MX
          # dvakrát, ne na kyperskou ligu
    # Jižní Amerika
    71,   # Brasileirao Serie A
    72,   # Brasileirao Serie B
    128,  # Argentine Primera Division
    129,  # Argentine Primera B Nacional
    # Uruguay má sezónu rozdělenou na Apertura/Clausura, appka proto
    # potřebuje OBĚ ID (žádné jednotné "Primera Division" u API neexistuje).
    # Dřív appka měla jen 239, což je ve skutečnosti KOLUMBIE (ponechána
    # níže s opraveným popiskem, je to reálná a sázkově zajímavá liga).
    268,  # Primera División - Apertura (Uruguay)
    270,  # Primera División - Clausura (Uruguay)
    239,  # Primera A (Colombia)
    265,  # Primera División (Chile) — dřív appka měla omylem 242, což je
          # Ekvádor (Liga Pro), ne Chile
    242,  # Liga Pro (Ekvádor) — appka tu dřív měla jen omylem jako popisek
          # u Chile výš, appka ho teď přidává jako správnou, samostatnou ligu
    281,  # Primera División (Peru)
    250,  # División Profesional - Apertura (Paraguay)
    252,  # División Profesional - Clausura (Paraguay)
    344,  # Primera División (Bolívie)
    # USA/Kanada
    253,  # MLS
    255,  # USL Championship — dřív appka měla omylem 254, což je ženská NWSL
    # Mexiko
    262,  # Liga MX
    # Japonsko
    98,   # J1 League
    # Jižní Korea
    292,  # K League 1
    # Austrálie
    188,  # A-League
    # Saúdská Arábie
    307,  # Pro League
    # Egypt
    233,  # Premier League
    # Čína VYNECHÁNA ÚMYSLNĚ — appka sem dřív omylem vložila ID 169 se
    # stejnou poznámkou o nejistotě jako u Švýcarska výš. 169 je ve
    # skutečnosti SPRÁVNÉ ID čínské Super League (potvrzeno přímo přes
    # /leagues), jenže čínské zápasy appce reálně chodily na Tipsportu
    # nedostupné (nahlášeno uživatelem 2x) — appka proto celou ligu radši
    # úplně vynechá, než aby hádala další ID.
    # Evropské poháry
    2,    # Champions League
    3,    # Europa League
    848,  # Conference League
    531,  # UEFA Super Cup
    # Mezinárodní — appka tu dřív měla několik ID přehozených (ověřeno
    # živě přes /leagues): 6 byl ve skutečnosti Africký pohár národů (ne
    # Nations League — ta je celá jen pod ID 5), 29 byla kvalifikace MS
    # Afrika (ne Africký pohár), 30 byla kvalifikace MS Asie (ne Asijský
    # pohár) a 13 byla Copa Libertadores, klubová soutěž (ne Copa America,
    # národní týmy). Appka teď nechává ponechané ID s opraveným popiskem
    # (jsou to reálné, sázkově zajímavé soutěže) a přidává správné ID pro
    # to, co appka PŮVODNĚ chtěla.
    1,    # World Cup
    4,    # Euro
    960,  # Euro - kvalifikace — appka měla kvalifikaci na MS, ale na Euro
          # kvalifikaci appka neměla vůbec nic
    5,    # UEFA Nations League
    10,   # Friendlies (mezinárodní přátelská)
    32,   # World Cup Qualifiers Europe
    34,   # World Cup Qualifiers South America
    29,   # World Cup Qualifiers Africa
    6,    # Africa Cup of Nations
    30,   # World Cup Qualifiers Asia
    7,    # Asian Cup
    13,   # CONMEBOL Libertadores
    11,   # CONMEBOL Sudamericana — appka tu dřív měla jen Libertadores,
          # Sudamericana appce úplně chyběla, i když je to reálná,
          # sázkově zajímavá klubová soutěž vedle ní
    9,    # CONMEBOL Copa America
    22,   # CONCACAF Gold Cup
    772,  # Leagues Cup (CONCACAF/MLS)
    15,   # FIFA Club World Cup — appka tu dřív neměla, appka to ověřila
          # křížově přes the-odds-api (appka odsud bere tržní kurzy) —
          # patří tam mezi soutěže s reálným sázkovým pokrytím
}



# API-Football označuje klubové sezóny startovním rokem — sezóna 2025/26
# (běží srpen-květen) má season=2025, i v lednu/červnu 2026, kdy už je
# aktuální kalendářní rok jiný. Mezinárodní turnaje vázané na jeden
# kalendářní rok (MS, EURO...) appka naopak bere přímo aktuálním rokem.
SINGLE_CALENDAR_YEAR_COMPETITIONS: set[int] = {1}  # MS (World Cup) — doplň EURO/Copa América apod., pokud appka začne sledovat i je


def _season_year_for_league(league_id: int, today: date) -> int:
    if league_id in SINGLE_CALENDAR_YEAR_COMPETITIONS:
        return today.year
    return today.year if today.month >= 7 else today.year - 1


class _RateLimiter:
    """
    Hlídá minimální rozestup mezi voláními API-Football bez ohledu na to,
    KOLIK vláken posílá požadavky souběžně — appka teď zpracovává zápasy
    paralelně (viz FIXTURE_ENRICHMENT_WORKERS v backend_api.py), a bez
    téhle brzdy by vlákna nezávisle na sobě klidně vystřelila víc
    požadavků ve stejné vteřině, než plán dovoluje (appka tohle naživo
    ověřila — Pro plán 5 req/s, appka dostávala zpátky 'Too many
    requests'). Appka jede vždy jen na cca 80 % povoleného stropu plánu —
    malá rezerva proti drobným časovým nepřesnostem.
    """
    def __init__(self, max_per_second: float = 4.0):
        self._min_interval = 1.0 / max_per_second
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_call = time.monotonic()


# Appka chce JEDEN limiter pro VŠECHNY instance APIFootballProvider (ne
# jeden per instanci) — limit je vázaný na klíč/účet, ne na to, kolikrát
# appka v kódu provider vytvoří.
#
# Mega plán API-Football: 900 req/min = 15 req/s tvrdý strop, 150 000
# req/den (appka dřív jela na Pro — 5 req/s, 7 500/den). Appka jede na
# 12 req/s (80 % z 15), stejná bezpečnostní rezerva jako dřív na Pro.
_api_football_rate_limiter = _RateLimiter(max_per_second=12.0)


class APIFootballProvider(SportsDataProvider):
    def __init__(self, api_key: Optional[str] = None, cache_ttl_seconds: int = 300):
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
        # Denní cron appky teď generuje 12+ tiketů v jednom běhu (viz
        # /admin/daily-tickets), což i s _api_football_rate_limiter
        # občas krátce překročí limit požadavků za minutu appky u
        # API-Football (429). Appka to zkusí pár krát znovu s prodlevou,
        # než to appka celé vzdá — jedno 429 uprostřed běhu appku dřív
        # celou shodilo (HTTP 500), i když šlo jen o dočasné zpomalení.
        last_error = None
        for attempt in range(3):
            _api_football_rate_limiter.wait()
            resp = requests.get(f"{API_FOOTBALL_BASE_URL}{path}", headers=self._headers(), params=params, timeout=8)
            if resp.status_code == 429:
                last_error = requests.exceptions.HTTPError(f"429 Client Error: Too Many Requests for url: {resp.url}", response=resp)
                retry_after = resp.headers.get("Retry-After")
                wait_seconds = float(retry_after) if retry_after else 3.0 * (attempt + 1)
                time.sleep(wait_seconds)
                continue
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("errors"):
                raise RuntimeError(f"API-Football vrátilo chybu: {payload['errors']}")
            return payload.get("response", [])
        raise last_error

    def get_upcoming_matches(self, sport: Sport, days_ahead: int,
                             custom_date: Optional[str] = None,
                             date_from: Optional[str] = None,
                             date_to: Optional[str] = None) -> list[dict]:
        if sport != Sport.FOOTBALL:
            raise NotImplementedError("APIFootballProvider pokrývá jen fotbal.")

        # Urči seznam dní ke stažení
        if custom_date:
            # Jeden konkrétní den
            dates = [custom_date]
        elif date_from and date_to:
            # Rozsah od-do
            from datetime import datetime as _dt
            start = _dt.fromisoformat(date_from).date()
            end = _dt.fromisoformat(date_to).date()
            dates = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
            dates = dates[:7]  # max 7 dní v rozsahu
        else:
            # Výchozí chování — od dneška N dní
            today = date.today()
            dates = [(today + timedelta(days=i)).isoformat() for i in range(days_ahead + 1)]

        today_iso = date.today().isoformat()
        cache_key = f"upcoming:{today_iso}:{','.join(dates)}"

        # Nejdřív zkus in-memory cache (rychlé, žádné DB volání)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Pak zkus persistentní DB cache — přežije restart serveru
        # (Render free tier usíná a in-memory cache se smaže)
        try:
            import db as _db
            db_cached = _db.cache_get(cache_key)
            if db_cached is not None:
                print(f"[cache] Zápasy načteny z DB cache (klíč: {cache_key})")
                self._cache.set(cache_key, db_cached)
                return db_cached
        except Exception as e:
            print(f"[cache] DB cache nedostupná, pokračuju bez ní: {e}")

        # Appka dřív limit rovnoměrně DĚLILA přes všechny požadované dny
        # (MAX_FIXTURES_PER_REQUEST // len(dates)) — na řídký den (pondělí,
        # pár soutěží) to appce zbytečně ubralo z bohatého dne (sobota),
        # i když ten bohatý den měl kandidátů dost sám o sobě. Appka teď
        # jde po dnech PO POŘADÍ (dnes, zítra, ...) a bere z KAŽDÉHO,
        # co zbývá ze stropu MAX_FIXTURES_PER_REQUEST — na další den appka
        # sáhne, jen když jí ten předchozí nedal dost. Běžná sobota tak
        # appce klidně stačí celá sama, bez zbytečných volání na další dny.
        fixtures: list[dict] = []
        today_str = date.today().isoformat()
        now_utc = datetime.utcnow()

        def is_upcoming(f: dict) -> bool:
            status = f.get("fixture", {}).get("status", {}).get("short", "NS")
            if status in ("FT", "AET", "PEN", "ABD", "CANC", "PST", "1H", "2H", "HT", "ET", "BT", "P", "LIVE"):
                return False
            kickoff = f.get("fixture", {}).get("date", "")
            if kickoff:
                try:
                    from datetime import timezone as _tz
                    import zoneinfo
                    ko = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
                    ko_utc = ko.astimezone(_tz.utc).replace(tzinfo=None)
                    if (ko_utc - now_utc).total_seconds() < 30 * 60:
                        return False
                    # Filtr nočních zápasů 0:00-8:00 CET
                    try:
                        cet = zoneinfo.ZoneInfo("Europe/Prague")
                        ko_cet = ko.astimezone(cet)
                        if 0 <= ko_cet.hour < 8:
                            return False
                    except Exception:
                        pass
                except Exception:
                    pass
            return True

        for day_str in dates:
            remaining = MAX_FIXTURES_PER_REQUEST - len(fixtures)
            if remaining <= 0:
                break  # strop už je plný — appka nemá důvod volat další (dražší) dny
            day_fixtures = self._get("/fixtures", {"date": day_str})
            day_fixtures = [f for f in day_fixtures if f.get("league", {}).get("id") in TIPSPORT_LEAGUE_IDS]
            # Budoucí dny — filtruj jen NS (nezačalo), dnes — filtruj podle času
            if day_str > today_str:
                def not_night(f):
                    kickoff = f.get("fixture", {}).get("date", "")
                    if not kickoff: return True
                    try:
                        import zoneinfo
                        from datetime import timezone as _tz
                        ko = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
                        cet = zoneinfo.ZoneInfo("Europe/Prague")
                        ko_cet = ko.astimezone(cet)
                        if 0 <= ko_cet.hour < 8: return False
                    except Exception:
                        pass
                    return f.get("fixture", {}).get("status", {}).get("short", "NS") == "NS"
                day_fixtures = [f for f in day_fixtures if not_night(f)]
            else:
                day_fixtures = [f for f in day_fixtures if is_upcoming(f)]
            day_fixtures.sort(key=lambda f: f.get("fixture", {}).get("date", ""))
            fixtures.extend(day_fixtures[:remaining])
            time.sleep(0.3)

        # Ulož do obou cache — in-memory pro tuto session, DB pro příští restart
        self._cache.set(cache_key, fixtures)
        try:
            import db as _db
            _db.cache_set(cache_key, fixtures, ttl_seconds=30 * 60)  # 30 minut — zápasy průběžně začínají
            print(f"[cache] Zápasy uloženy do DB cache ({len(fixtures)} zápasů, TTL 4h)")
        except Exception as e:
            print(f"[cache] Uložení do DB cache selhalo: {e}")

        return fixtures

    def get_team_statistics(self, sport: Sport, team_id: str, league_id: Optional[str] = None) -> dict:
        cache_key = f"team_stats:{team_id}:{league_id}"
        # In-memory cache
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        # DB cache — statistiky týmu se mění max. jednou týdně, 24h TTL je bezpečné
        try:
            import db as _db
            db_cached = _db.cache_get(cache_key)
            if db_cached is not None:
                # appka ukládá dict zabalený v listu [data] — vytáhni zpátky
                result = db_cached[0] if isinstance(db_cached, list) and db_cached else db_cached if isinstance(db_cached, dict) else {}
                self._cache.set(cache_key, result)
                return result
        except Exception:
            pass

        if not league_id:
            return {}
        season = _season_year_for_league(int(league_id), date.today())
        response = self._get("/teams/statistics", {"team": team_id, "season": season, "league": league_id})
        data = response if isinstance(response, dict) else (response[0] if response else {})
        self._cache.set(cache_key, data)
        try:
            import db as _db
            _db.cache_set(cache_key, [data] if data else [], ttl_seconds=24 * 3600)
        except Exception:
            pass
        return data

    def get_recent_form(self, team_id: str, last: int = 5) -> list[dict]:
        """
        Posledních `last` dokončených zápasů týmu — slouží k vážení nedávné
        formy (viz adapt_recent_form_goals). POZOR: tohle je DALŠÍ API
        dotaz navíc k team_stats a odds, takže per zápas appka teď volá
        API-Football 5x místo 3x (2x stats + 2x forma + 1x kurzy). Na
        zdarma plánu (100 dotazů/den) tohle rychle vyčerpá limit — vyplatí
        se to hlavně po přechodu na placený plán s vyšším limitem.
        """
        cache_key = f"recent_form:{team_id}:{last}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            import db as _db
            db_cached = _db.cache_get(cache_key)
            if db_cached is not None:
                self._cache.set(cache_key, db_cached)
                return db_cached
        except Exception:
            pass
        fixtures = self._get("/fixtures", {"team": team_id, "last": last, "status": "FT"})
        self._cache.set(cache_key, fixtures)
        try:
            import db as _db
            _db.cache_set(cache_key, fixtures, ttl_seconds=6 * 3600)  # forma se mění po odehraném zápase
        except Exception:
            pass
        return fixtures

    def get_pre_match_odds(self, match_id: str) -> dict:
        cache_key = f"odds:{match_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            import db as _db
            db_cached = _db.cache_get(cache_key)
            if db_cached is not None:
                self._cache.set(cache_key, db_cached[0] if db_cached else {})
                return db_cached[0] if db_cached else {}
        except Exception:
            pass
        response = self._get("/odds", {"fixture": match_id})
        data = response[0] if response else {}
        self._cache.set(cache_key, data)
        try:
            import db as _db
            _db.cache_set(cache_key, [data] if data else [], ttl_seconds=2 * 3600)  # kurzy se hýbají, 2h stačí
        except Exception:
            pass
        return data

    def get_fixture_result(self, match_id: str) -> dict:
        """
        Finální (nebo aktuální) skóre a stav konkrétního zápasu — appka
        to používá k dosettlování tiketů po skončení utkání. Volá se pro
        KAŽDOU nohu KAŽDÉHO nevyřešeného tiketu při KAŽDÉM otevření
        Historie (viz /tickets/saved) — bez cache appka tenhle jeden
        zápas zjišťovala znovu při každém dalším otevření appky (u
        libovolného uživatele, co ho má v tiketu), i kdyby se od
        posledního dotazu vůbec nic nezměnilo. Krátké TTL (appka chce
        včasný výsledek po skončení zápasu, ne zastaralý o hodiny).
        """
        cache_key = f"fixture_result:{match_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            import db as _db
            db_cached = _db.cache_get(cache_key)
            if db_cached is not None:
                result = db_cached[0] if isinstance(db_cached, list) and db_cached else db_cached if isinstance(db_cached, dict) else {}
                self._cache.set(cache_key, result)
                return result
        except Exception:
            pass
        response = self._get("/fixtures", {"id": match_id})
        data = response[0] if response else {}
        self._cache.set(cache_key, data)
        try:
            import db as _db
            _db.cache_set(cache_key, [data] if data else [], ttl_seconds=10 * 60)  # 10 minut — appka chce brzy vidět čerstvý výsledek, ne ho jen šetřit navěky
        except Exception:
            pass
        return data

    def get_fixture_statistics(self, match_id: str) -> list[dict]:
        """
        Statistiky (mj. počet karet) k jednomu odehranému zápasu — appka
        tohle volá jen při dosettlování Over Cards tiketů (viz
        _settle_one_leg v backend_api.py), ne u každé nohy každého
        tiketu, ať appka zbytečně neplýtvá API budgetem na trhy, co ho
        nepotřebují.
        """
        cache_key = f"fixture_statistics:{match_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            import db as _db
            db_cached = _db.cache_get(cache_key)
            if db_cached is not None:
                self._cache.set(cache_key, db_cached)
                return db_cached
        except Exception:
            pass
        response = self._get("/fixtures/statistics", {"fixture": match_id})
        self._cache.set(cache_key, response)
        try:
            import db as _db
            _db.cache_set(cache_key, response, ttl_seconds=10 * 60)
        except Exception:
            pass
        return response

    def get_injuries(self, match_id: str) -> list[dict]:
        """
        Hráči nahlášení jako zranění/vyloučení pro konkrétní zápas. POZOR:
        appka z toho umí spočítat jen POČET jmen, ne jejich důležitost pro
        tým — viz injury_goal_adjustment_factor. Volá se při KAŽDÉM
        generování tiketu pro KAŽDÉHO kandidáta zvlášť, bez cache appka
        tohle zjišťovala znovu při každém požadavku, i napříč různými
        uživateli se stejným zápasem v poolu.
        """
        cache_key = f"injuries:{match_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            import db as _db
            db_cached = _db.cache_get(cache_key)
            if db_cached is not None:
                self._cache.set(cache_key, db_cached)
                return db_cached
        except Exception:
            pass
        data = self._get("/injuries", {"fixture": match_id})
        self._cache.set(cache_key, data)
        try:
            import db as _db
            _db.cache_set(cache_key, data, ttl_seconds=3 * 3600)  # sestava/zranění se do zápasu obvykle nemění po hodinách
        except Exception:
            pass
        return data

    def get_standings(self, league_id: str, season: int) -> list[dict]:
        """
        Aktuální tabulka soutěže — appka to používá k odhadu, jestli už
        pro některý z týmů nejde "o nic" (viz adapt_standings_for_motivation).
        Volá se při KAŽDÉM generování tiketu pro každou ligu v poolu
        (appka to sice v rámci JEDNOHO požadavku sdílí přes standings_cache
        v backend_api.py, ale bez tyhle DB cache appka tabulku znovu
        stahovala při KAŽDÉM DALŠÍM požadavku i pro tu samou ligu). Tabulka
        soutěže se mění jen po odehraných kolech, ne v řádu hodin.
        """
        cache_key = f"standings:{league_id}:{season}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            import db as _db
            db_cached = _db.cache_get(cache_key)
            if db_cached is not None:
                self._cache.set(cache_key, db_cached)
                return db_cached
        except Exception:
            pass
        response = self._get("/standings", {"league": league_id, "season": season})
        try:
            data = response[0]["league"]["standings"][0]
        except (IndexError, KeyError, TypeError):
            data = []
        self._cache.set(cache_key, data)
        try:
            import db as _db
            _db.cache_set(cache_key, data, ttl_seconds=24 * 3600)  # tabulka se mění jen po odehraných kolech
        except Exception:
            pass
        return data


# ---------------------------------------------------------------------
# Adaptéry: skutečný JSON tvar API-Football -> generické dicty, které
# normalize_to_match_input / normalize_to_match_snapshot (výše) očekávají.
# ---------------------------------------------------------------------
def adapt_fixture_result(fixture: dict) -> dict:
    """
    fixture = jeden prvek z get_fixture_result() (/fixtures?id=X).
    Appka to používá k dosettlování tiketů — is_finished musí být True
    a góly musí existovat, jinak appka tiket nechá 'pending'.
    """
    if not fixture:
        return {"is_finished": False, "home_goals": None, "away_goals": None, "ht_home_goals": None, "ht_away_goals": None}
    status_short = fixture.get("fixture", {}).get("status", {}).get("short", "")
    goals = fixture.get("goals", {})
    # Poločasové skóre appka potřebuje kvůli settlementu ht_over_goals/
    # ht_under_goals (viz evaluate_selection_outcome) — API-Football ho
    # vrací zadarmo v tom samém /fixtures response jako celkové skóre,
    # appka na to nepotřebuje žádné další volání navíc.
    halftime = fixture.get("score", {}).get("halftime", {})
    return {
        "is_finished": status_short in ("FT", "AET", "PEN"),
        "home_goals": goals.get("home"),
        "away_goals": goals.get("away"),
        "ht_home_goals": halftime.get("home"),
        "ht_away_goals": halftime.get("away"),
    }


def adapt_fixture_card_count(statistics: list[dict]) -> Optional[int]:
    """
    statistics = odpověď z get_fixture_statistics() (/fixtures/statistics
    ?fixture=X) — appka sečte žluté i červené karty OBOU týmů. API appce
    u některých soutěží (hlavně nižších) statistiky vůbec neposílá —
    appka to pozná podle prázdné odpovědi/chybějícího typu a vrátí
    None, ať appka tiket nechá 'pending' místo špatného odhadu.
    """
    if not statistics:
        return None
    total = 0
    found_any = False
    for team_stats in statistics:
        for stat in (team_stats.get("statistics") or []):
            stat_type = (stat.get("type") or "").strip().lower()
            if stat_type in ("yellow cards", "red cards"):
                value = stat.get("value")
                if value is not None:
                    try:
                        total += int(value)
                        found_any = True
                    except (ValueError, TypeError):
                        pass
    return total if found_any else None


def adapt_api_football_fixture(fixture: dict) -> dict:
    """fixture = jeden prvek z `response` endpointu /fixtures."""
    return {
        "id": fixture["fixture"]["id"],
        "home_team": fixture["teams"]["home"]["name"],
        "away_team": fixture["teams"]["away"]["name"],
        "home_team_id": fixture["teams"]["home"]["id"],
        "away_team_id": fixture["teams"]["away"]["id"],
        # API-Football tohle vrací zadarmo u každého zápasu — zatím se
        # nepoužívá k úpravě pravděpodobnosti, jen se nese dál (viz
        # MatchInput.referee), dokud nebudeme mít historii karet per rozhodčí.
        "referee": fixture["fixture"].get("referee"),
        # Liga/soutěž — appka to používá k odhadu korelace mezi výběry ve
        # stejném kombo tiketu (viz TicketGenerator._apply_correlation_discount).
        "league": fixture.get("league", {}).get("name", ""),
        "country": fixture.get("league", {}).get("country", ""),
        # ID ligy + sezóna — appka to potřebuje k dotahování tabulky soutěže
        # (viz get_standings / adapt_standings_for_motivation).
        "league_id": fixture.get("league", {}).get("id"),
        "season": fixture.get("league", {}).get("season"),
        # Město stadionu + čas výkopu — vstup pro get_match_weather() níže.
        "venue_city": fixture["fixture"].get("venue", {}).get("city"),
        "kickoff_time": fixture["fixture"].get("date"),
    }


def adapt_api_football_team_stats(stats: dict) -> dict:
    """stats = `response` objekt z /teams/statistics."""
    goals_avg = (
        stats.get("goals", {}).get("for", {}).get("average", {}).get("total", "1.2")
        or "1.2"
    )
    # appka tohle pole ze stejné odpovědi dřív vůbec nečetla — model tak
    # počítal góly týmu jen z JEHO VLASTNÍHO útoku a ignoroval, jak dobrou
    # obranu má soupeř (viz _estimate_expected_goals). API ho přitom
    # appce posílá v tom samém volání, appka ho jen zahazovala.
    goals_conceded_avg = (
        stats.get("goals", {}).get("against", {}).get("average", {}).get("total", "1.3")
        or "1.3"
    )
    yellow_cards = stats.get("cards", {}).get("yellow", {})
    total_yellow = sum(
        int(v.get("total") or 0) for v in yellow_cards.values() if isinstance(v, dict)
    )
    played = stats.get("fixtures", {}).get("played", {}).get("total") or 1
    return {
        "avg_goals_scored_last_10": float(goals_avg),
        "avg_goals_conceded_last_10": float(goals_conceded_avg),
        "avg_cards_last_10": round(total_yellow / played, 2),
        "games_played": played,
    }


def is_live_market_blocked(odds_response: dict) -> bool:
    """
    Vrátí True, pokud bookmaker právě live sázení na tenhle zápas pozastavil
    (typicky pár sekund po nebezpečné situaci, dokud se nevyjasní výsledek).
    POZOR: přesný tvar pole 'blocked'/'stopped' v odpovědi /odds/live není
    z dokumentace API-Football (beta endpoint) 100% jistý — appka při
    chybějícím poli bezpečně předpokládá, že trh pozastavený NENÍ (raději
    appka jednou ukáže kurz, co se mezitím nepatrně posunul, než aby kvůli
    nejistotě umlčela všechny signály).
    """
    return bool(odds_response.get("blocked", False) or odds_response.get("stopped", False))


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 == 1 else (s[mid - 1] + s[mid]) / 2


def adapt_api_football_odds(odds_response: dict) -> dict:
    """
    odds_response = jeden prvek `response` z /odds (pro daný fixture).

    Appka neber jen jednoho bookmakera (dřív "Bet365, nebo první dostupný")
    — agreguje napříč VŠEMI bookmakery v odpovědi. Medián je robustnější
    vůči jednomu odchýlenému bookmakerovi než průměr nebo "první v pořadí".
    Tam, kde appka má obě strany trhu (Home/Draw/Away, Over/Under, Yes/No)
    napříč víc bookmakery, navíc spočítá de-vigovanou tržní pravděpodobnost
    z těch mediánových cen — funguje to jako market-consensus kontrola i
    bez druhého (the-odds-api) zdroje dat, viz _enrich_with_market_odds.
    """
    result: dict = {
        "match_winner": {}, "over_goals": {}, "under_goals": {}, "btts_yes": None, "over_cards": {},
        "market_implied_probabilities": {}, "bookmaker_count": len(odds_response.get("bookmakers", [])),
    }
    bookmakers = odds_response.get("bookmakers", [])
    if not bookmakers:
        return result

    home_prices, draw_prices, away_prices = [], [], []
    btts_yes_prices, btts_no_prices = [], []
    over_goals_prices: dict[float, list[float]] = {}
    under_goals_prices: dict[float, list[float]] = {}
    over_cards_prices: dict[float, list[float]] = {}

    for bm in bookmakers:
        for bet in bm.get("bets", []):
            name = bet.get("name")
            values = bet.get("values", [])
            if name == "Match Winner":
                for v in values:
                    try:
                        odd = float(v["odd"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    if v.get("value") == "Home":
                        home_prices.append(odd)
                    elif v.get("value") == "Draw":
                        draw_prices.append(odd)
                    elif v.get("value") == "Away":
                        away_prices.append(odd)
            elif name == "Goals Over/Under":
                for v in values:
                    val = str(v.get("value", ""))
                    try:
                        odd = float(v["odd"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    if val.startswith("Over "):
                        over_goals_prices.setdefault(float(val.replace("Over ", "")), []).append(odd)
                    elif val.startswith("Under "):
                        under_goals_prices.setdefault(float(val.replace("Under ", "")), []).append(odd)
            elif name == "Both Teams Score":  # POZN.: přesný název trhu u API-Football neověřen, best-effort
                for v in values:
                    try:
                        odd = float(v["odd"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    if v.get("value") == "Yes":
                        btts_yes_prices.append(odd)
                    elif v.get("value") == "No":
                        btts_no_prices.append(odd)
            elif name == "Cards Over/Under":
                for v in values:
                    val = str(v.get("value", ""))
                    try:
                        odd = float(v["odd"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    if val.startswith("Over "):
                        over_cards_prices.setdefault(float(val.replace("Over ", "")), []).append(odd)

    if home_prices:
        result["match_winner"]["favorite"] = _median(home_prices)
    if home_prices and draw_prices and away_prices:
        probs = devig_market([
            ("home", _median(home_prices)), ("draw", _median(draw_prices)), ("away", _median(away_prices)),
        ])
        result["market_implied_probabilities"]["match_winner:home"] = probs["home"]
        result["market_implied_probabilities"]["match_winner:away"] = probs["away"]

    # Vyřazuje čtvrtinové linie (1.75, 2.25, ...) — API-Football je sice
    # vrací (agregace zahraničních bookmakerů), ale Tipsport a další čeští
    # sázkaři běžně nabízí jen celé/půlené linie (1.5, 2.0, 2.5...). Tiket
    # s "over 1.75" pak uživatel na Tipsportu nenajde.
    def _is_standard_line(threshold: float) -> bool:
        return (threshold * 4) % 2 == 0  # zůstanou jen násobky 0.5

    for threshold, prices in over_goals_prices.items():
        if not _is_standard_line(threshold):
            continue
        result["over_goals"][threshold] = _median(prices)
        if threshold in under_goals_prices:
            under_price = _median(under_goals_prices[threshold])
            p_over, p_under = devig_two_way(_median(prices), under_price)
            result["market_implied_probabilities"][f"over_goals:over_{threshold}"] = p_over
            result["under_goals"][threshold] = under_price
            result["market_implied_probabilities"][f"over_goals:under_{threshold}"] = p_under

    for threshold, prices in over_cards_prices.items():
        if not _is_standard_line(threshold):
            continue
        result["over_cards"][threshold] = _median(prices)

    if btts_yes_prices:
        result["btts_yes"] = _median(btts_yes_prices)
        if btts_no_prices:
            p_yes, _ = devig_two_way(_median(btts_yes_prices), _median(btts_no_prices))
            result["market_implied_probabilities"]["btts:yes"] = p_yes

    return result


# =======================================================================
# Factory — vybere providera dle sportu (definováno až tady, na konci,
# protože potřebuje znát všechny třídy výše)
# =======================================================================
# SINGLETON PROVIDERS — jedinou instanci pro celý lifetime aplikace
_PROVIDER_CACHE: dict[Sport, SportsDataProvider] = {}


def get_provider(sport: Sport) -> SportsDataProvider:
    if sport in _PROVIDER_CACHE:
        return _PROVIDER_CACHE[sport]

    if sport == Sport.FOOTBALL:
        provider = APIFootballProvider(cache_ttl_seconds=3600)  # 1 hodina in-memory
    elif sport == Sport.BASKETBALL:
        provider = APISportsDirectProvider(sport_path="basketball", cache_ttl_seconds=3600)
    elif sport == Sport.HOCKEY:
        provider = APISportsDirectProvider(sport_path="hockey", cache_ttl_seconds=3600)
    elif sport == Sport.TENNIS:
        provider = APITennisProvider(cache_ttl_seconds=3600)
    else:
        raise NotImplementedError(f"Pro sport '{sport.value}' chybí napojený provider.")

    _PROVIDER_CACHE[sport] = provider
    return provider
