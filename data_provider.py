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
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from probability_model import MatchInput, Sport, MarketType, devig_market, devig_two_way, MarketEvaluator
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
    home_xg = _estimate_expected_goals(home_stats, is_home=True, recency_weighted_avg=home_recent_form, adjustment_factor=home_factor)
    away_xg = _estimate_expected_goals(away_stats, is_home=False, recency_weighted_avg=away_recent_form, adjustment_factor=away_factor)
    expected_cards = _estimate_expected_cards(home_stats, away_stats)

    # FALLBACK KURZY: Pokud API-Football nevrátil kurzy, estimuj z model pravděpodobnosti
    favorite_odds = odds_raw.get("match_winner", {}).get("favorite", None)
    if favorite_odds is None or favorite_odds < 1.01:
        # API-Football nevrátil kurzy - estimuj z Poissonova modelu
        winner_probs = MarketEvaluator.match_winner_probabilities(home_xg, away_xg)
        favorite_prob = max(winner_probs.values())  # Nejvyšší pravděpodobnost
        favorite_odds = round(1.0 / max(favorite_prob, 0.01), 2)  # Převeď na kurz
    
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
            expected_cards=expected_cards,
            home_games_played=home_stats.get("games_played", 0),
            away_games_played=away_stats.get("games_played", 0),
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
            over_goals_odds=odds_raw.get("over_goals", {}),     # {2.5: 1.85, 3.5: 2.60, ...}
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


LEAGUE_AVERAGE_GOALS_PER_TEAM = 1.3  # rozumný univerzální odhad přes evropské ligy
SHRINKAGE_PSEUDO_GAMES = 5  # kolik "fiktivních" zápasů váží ligový průměr vůči datům týmu
RECENCY_BLEND_WEIGHT = 0.6  # váha posledních zápasů vs. sezónního průměru, když je forma dostupná

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
    """
    Počet dní od posledního odehraného zápasu týmu do tohoto výkopu —
    appka to počítá z dat, co už tahá pro recency formu (get_recent_form),
    žádný extra API dotaz navíc. Vrací None, pokud appka nemá dostatek dat.
    """
    if not recent_fixtures:
        return None
    try:
        last_match_date = datetime.fromisoformat(recent_fixtures[0]["fixture"]["date"].replace("Z", "+00:00"))
        kickoff_date = datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00"))
        return max((kickoff_date - last_match_date).days, 0)
    except (KeyError, ValueError, TypeError, IndexError):
        return None


def _estimate_expected_goals(
    team_stats: dict, is_home: bool, recency_weighted_avg: Optional[float] = None,
    adjustment_factor: float = 1.0,
) -> float:
    """
    Zjednodušený xG odhad: průměr vstřelených gólů, upravený o domácí/
    venkovní výhodu, se třemi vrstvami opatrnosti navrch:

    1) Shrinkage na malém vzorku — na začátku sezóny (málo odehraných
       zápasů) je sezónní průměr statisticky nespolehlivý (velký šum).
       "Stáhneme" ho blíž k ligovému průměru úměrně tomu, kolik dat tým
       má; s přibývajícími zápasy korekce postupně mizí.
    2) Vážení nedávné formy — pokud `recency_weighted_avg` je dostupný
       (poslední zápasy vážené víc než starší, viz data_provider.
       adapt_recent_form_goals), zkombinuje se se sezónním průměrem,
       aby appka reagovala na aktuální formu, ne jen na celosezónní stav.
    3) adjustment_factor — souhrnný multiplikátor počasí × zranění ×
       odpočinku × motivace (viz *_adjustment_factor funkce výše); bez
       jakýchkoli dat zůstává 1.0 = beze změny.

    V produkci by šlo nahradit plnohodnotným xG modelem (Dixon-Coles,
    Poisson regrese s útočnou/obrannou silou týmu) — to ale vyžaduje
    samostatný (a placený) zdroj dat, viz poznámka v probability_model.py.
    """
    avg_goals_scored = team_stats.get("avg_goals_scored_last_10", 1.2)
    games_played = team_stats.get("games_played", 0)

    shrunk_avg = (
        games_played * avg_goals_scored + SHRINKAGE_PSEUDO_GAMES * LEAGUE_AVERAGE_GOALS_PER_TEAM
    ) / (games_played + SHRINKAGE_PSEUDO_GAMES)

    if recency_weighted_avg is not None:
        shrunk_avg = RECENCY_BLEND_WEIGHT * recency_weighted_avg + (1 - RECENCY_BLEND_WEIGHT) * shrunk_avg

    home_advantage_factor = 1.10 if is_home else 0.92
    return round(shrunk_avg * home_advantage_factor * adjustment_factor, 2)


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

    def __init__(self, api_key: Optional[str] = None, cache_ttl_seconds: int = 300):
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
        "btts_yes_odds": None,
        "bookmaker_count": len(event.get("bookmakers", [])),
    }
    bookmakers = event.get("bookmakers", [])
    if not bookmakers:
        return result

    home_probs, away_probs, home_prices = [], [], []
    btts_probs, btts_prices = [], []
    totals_by_threshold: dict[float, list[tuple[float, float]]] = {}  # threshold -> [(cena_over, p_over), ...]

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
                p_over, _ = devig_two_way(over_o["price"], under_o["price"])
                totals_by_threshold.setdefault(over_o["point"], []).append((over_o["price"], p_over))

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
        result["over_odds"] = _median([p for p, _ in prices_and_probs])
        result["over_probability"] = sum(p for _, p in prices_and_probs) / len(prices_and_probs)

    return result

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
# straně Render/Cloudflare. 40 zápasů × ~11 volání ≈ 440 požadavků na
# jedno generování — na Pro plánu (7 500/den) appka snese kolem 15-16
# takových generování za den, než narazí na denní limit.
MAX_FIXTURES_PER_REQUEST = 100  # Bezpečný limit - vejde se do 7,500 daily quota

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
    169,  # Swiss Super League
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
    # Izrael
    384,  # Premier League
    # Kypr
    262,  # First Division (Cyprus - pozn: ID může být jiné)
    # Jižní Amerika
    71,   # Brasileirao Serie A
    72,   # Brasileirao Serie B
    128,  # Argentine Primera Division
    129,  # Argentine Primera B Nacional
    239,  # Uruguay Primera Division
    242,  # Chile Primera Division
    # USA/Kanada
    253,  # MLS
    254,  # USL Championship
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
    # Čína
    169,  # Super League (China - pozn: ID může být jiné)
    # Evropské poháry
    2,    # Champions League
    3,    # Europa League
    848,  # Conference League
    531,  # UEFA Super Cup
    # Mezinárodní
    1,    # World Cup
    4,    # Euro
    6,    # Nations League
    5,    # UEFA Nations League playoff
    10,   # Friendlies (mezinárodní přátelská)
    32,   # World Cup Qualifiers Europe
    34,   # World Cup Qualifiers South America
    29,   # Africa Cup of Nations
    13,   # CONMEBOL Copa America
    30,   # AFC Asian Cup
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
    requests'). Appka jede na 4 req/s, ne na povolených 5 — malá
    rezerva proti drobným časovým nepřesnostem.
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
_api_football_rate_limiter = _RateLimiter(max_per_second=4.0)


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
        _api_football_rate_limiter.wait()
        resp = requests.get(f"{API_FOOTBALL_BASE_URL}{path}", headers=self._headers(), params=params, timeout=8)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"API-Football vrátilo chybu: {payload['errors']}")
        return payload.get("response", [])

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
                self._cache.set(cache_key, db_cached)
                return db_cached
        except Exception as e:
            pass  # DB cache nedostupná, pokračuj bez ní

        # Appka cílí na rovnoměrné rozdělení limitu PŘES VŠECHNY požadované
        # dny, ne na hladové vybrání jen těch nejbližších zápasů — jinak by
        # se appka u time_frame_days=3 klidně omezila jen na příštích pár
        # hodin a dny 2-3 by appka vůbec neviděla.
        per_day_limit = max(MAX_FIXTURES_PER_REQUEST // max(len(dates), 1), 1)
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
            fixtures.extend(day_fixtures[:per_day_limit])
            time.sleep(0.3)

        fixtures = fixtures[:MAX_FIXTURES_PER_REQUEST]

        # Ulož do obou cache — in-memory pro tuto session, DB pro příští restart
        self._cache.set(cache_key, fixtures)
        try:
            import db as _db
            _db.cache_set(cache_key, fixtures, ttl_seconds=30 * 60)  # 30 minut — zápasy průběžně začínají
        except Exception as e:
            pass  # Uložení do cache selhalo, pokračuj bez něj

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

    def get_live_match_stats(self, match_id: str) -> dict:
        stats = self._get("/fixtures/statistics", {"fixture": match_id})
        events = self._get("/fixtures/events", {"fixture": match_id})
        return {"statistics": stats, "events": events}

    def get_live_fixtures(self) -> list[dict]:
        """Vrátí všechny právě běžící zápasy — vstup pro poller Live Signal Engine."""
        return self._get("/fixtures", {"live": "all"})

    def get_live_odds(self, match_id: str) -> dict:
        """
        Živé (in-play) kurzy pro běžící zápas. POZOR: endpoint /odds/live
        byl u API-Football historicky beta funkce, kterou bylo nutné si
        zvlášť vyžádat přes chat na dashboard.api-football.com — pokud ho
        tvůj účet nemá povolený, tahle volání budou vracet prázdno a appka
        se tiše vrátí k chování bez kurzu (viz adapt_live_odds_for_signal).
        """
        response = self._get("/odds/live", {"fixture": match_id})
        return response[0] if response else {}

    def get_fixture_result(self, match_id: str) -> dict:
        """Finální (nebo aktuální) skóre a stav konkrétního zápasu — appka
        to používá k dosettlování tiketů po skončení utkání."""
        response = self._get("/fixtures", {"id": match_id})
        return response[0] if response else {}

    def get_injuries(self, match_id: str) -> list[dict]:
        """
        Hráči nahlášení jako zranění/vyloučení pro konkrétní zápas. POZOR:
        appka z toho umí spočítat jen POČET jmen, ne jejich důležitost pro
        tým — viz injury_goal_adjustment_factor.
        """
        return self._get("/injuries", {"fixture": match_id})

    def get_standings(self, league_id: str, season: int) -> list[dict]:
        """Aktuální tabulka soutěže — appka to používá k odhadu, jestli
        už pro některý z týmů nejde "o nic" (viz adapt_standings_for_motivation)."""
        response = self._get("/standings", {"league": league_id, "season": season})
        try:
            return response[0]["league"]["standings"][0]
        except (IndexError, KeyError, TypeError):
            return []


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
        return {"is_finished": False, "home_goals": None, "away_goals": None}
    status_short = fixture.get("fixture", {}).get("status", {}).get("short", "")
    goals = fixture.get("goals", {})
    return {
        "is_finished": status_short in ("FT", "AET", "PEN"),
        "home_goals": goals.get("home"),
        "away_goals": goals.get("away"),
    }


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
    yellow_cards = stats.get("cards", {}).get("yellow", {})
    total_yellow = sum(
        int(v.get("total") or 0) for v in yellow_cards.values() if isinstance(v, dict)
    )
    played = stats.get("fixtures", {}).get("played", {}).get("total") or 1
    return {
        "avg_goals_scored_last_10": float(goals_avg),
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


def adapt_live_odds_for_signal(odds_response: dict, team_side: str, bookmaker_name: str = "Bet365") -> Optional[float]:
    """
    Z odpovědi get_live_odds() vytáhne kurz na "Next Goal" pro danou stranu
    (home/away). Vrací None, pokud appka kurz nesežene — ať proto, že
    /odds/live na tvém účtu není povolený (viz poznámka u get_live_odds),
    nebo proto, že bookmaker zrovna live sázení na tenhle trh pozastavil
    (status "blocked": true v API-Football odpovědi).
    """
    bookmakers = odds_response.get("bookmakers", [])
    if not bookmakers:
        return None
    target = next((b for b in bookmakers if b.get("name") == bookmaker_name), bookmakers[0])

    next_goal_bet = next(
        (bet for bet in target.get("bets", []) if "next goal" in bet.get("name", "").lower()), None
    )
    if not next_goal_bet:
        return None

    wanted_label = "home" if team_side == "home" else "away"
    for value in next_goal_bet.get("values", []):
        if wanted_label in value.get("value", "").lower():
            try:
                return float(value["odd"])
            except (KeyError, ValueError, TypeError):
                return None
    return None


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
        "match_winner": {}, "over_goals": {}, "btts_yes": None, "over_cards": {},
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

    for threshold, prices in over_goals_prices.items():
        result["over_goals"][threshold] = _median(prices)
        if threshold in under_goals_prices:
            p_over, _ = devig_two_way(_median(prices), _median(under_goals_prices[threshold]))
            result["market_implied_probabilities"][f"over_goals:over_{threshold}"] = p_over

    for threshold, prices in over_cards_prices.items():
        result["over_cards"][threshold] = _median(prices)

    if btts_yes_prices:
        result["btts_yes"] = _median(btts_yes_prices)
        if btts_no_prices:
            p_yes, _ = devig_two_way(_median(btts_yes_prices), _median(btts_no_prices))
            result["market_implied_probabilities"]["btts:yes"] = p_yes

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
    # Pozn.: vlastní gól (Own Goal) API-Football typicky přiřadí k týmu
    # hráče, co ho dal, ne k týmu, kterému gól prospěl — pro účel "poznat,
    # že se skóre změnilo a appka má resetovat okno tlaku" (viz
    # MomentumFilter._handle_goal_change) tahle drobná nepřesnost nevadí.
    goals_home = sum(
        1 for e in live_raw.get("events", [])
        if e.get("type") == "Goal" and e.get("team", {}).get("name") == home_name
    )
    goals_away = sum(
        1 for e in live_raw.get("events", [])
        if e.get("type") == "Goal" and e.get("team", {}).get("name") == away_name
    )

    return {
        "minute": minute,
        "possession": {"home": _num(home_stats.get("Ball Possession")), "away": _num(away_stats.get("Ball Possession"))},
        "shots_on_target": {"home": _num(home_stats.get("Shots on Goal")), "away": _num(away_stats.get("Shots on Goal"))},
        "dangerous_attacks": {"home": _num(home_stats.get("Shots insidebox")), "away": _num(away_stats.get("Shots insidebox"))},
        "corners": {"home": _num(home_stats.get("Corner Kicks")), "away": _num(away_stats.get("Corner Kicks"))},
        "red_cards": {"home": red_cards_home, "away": red_cards_away},
        "goals": {"home": goals_home, "away": goals_away},
    }


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
