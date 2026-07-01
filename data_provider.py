"""
ApexSignal — Integrační vrstva pro sportovní data a kurzy
Modul: data_provider.py
"""

from __future__ import annotations

import os
import time
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from probability_model import MatchInput, Sport, MarketType, devig_market, devig_two_way
from momentum_filter import MatchSnapshot


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


class SportsDataProvider(ABC):
    @abstractmethod
    def get_upcoming_matches(self, sport: Sport, days_ahead: int) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def get_team_statistics(self, sport: Sport, team_id: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def get_pre_match_odds(self, match_id: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def get_live_match_stats(self, match_id: str) -> dict:
        raise NotImplementedError


class HttpSportsDataProvider(SportsDataProvider):
    def __init__(self, base_url=None, api_key=None, cache_ttl_seconds=300):
        self.base_url = base_url or os.environ.get("SPORTS_API_BASE_URL", "")
        self.api_key = api_key or os.environ.get("SPORTS_API_KEY", "")
        self._cache = InMemoryCache(ttl_seconds=cache_ttl_seconds)

    def _request(self, path, params=None):
        raise NotImplementedError

    def get_upcoming_matches(self, sport, days_ahead):
        cache_key = f"upcoming:{sport.value}:{days_ahead}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        data = self._request("/fixtures", {"sport": sport.value, "days": days_ahead})
        self._cache.set(cache_key, data)
        return data

    def get_team_statistics(self, sport, team_id):
        cache_key = f"team_stats:{sport.value}:{team_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        data = self._request(f"/teams/{team_id}/statistics", {"sport": sport.value})
        self._cache.set(cache_key, data)
        return data

    def get_pre_match_odds(self, match_id):
        return self._request(f"/odds/{match_id}")

    def get_live_match_stats(self, match_id):
        return self._request(f"/live/{match_id}")


def normalize_to_match_input(
    sport, fixture, home_stats, away_stats, odds_raw,
    home_recent_form=None, away_recent_form=None, weather=None,
    home_injury_count=0, away_injury_count=0,
    home_rest_days=None, away_rest_days=None,
    home_dead_rubber=False, away_dead_rubber=False,
    data_availability=None,
) -> MatchInput:
    weather_factor = weather_goal_adjustment_factor(weather)
    home_factor = (weather_factor * injury_goal_adjustment_factor(home_injury_count)
                   * rest_days_adjustment_factor(home_rest_days) * motivation_adjustment_factor(home_dead_rubber))
    away_factor = (weather_factor * injury_goal_adjustment_factor(away_injury_count)
                   * rest_days_adjustment_factor(away_rest_days) * motivation_adjustment_factor(away_dead_rubber))
    home_xg = _estimate_expected_goals(home_stats, is_home=True, recency_weighted_avg=home_recent_form, adjustment_factor=home_factor)
    away_xg = _estimate_expected_goals(away_stats, is_home=False, recency_weighted_avg=away_recent_form, adjustment_factor=away_factor)
    expected_cards = _estimate_expected_cards(home_stats, away_stats)

    return MatchInput(
        match_id=fixture["id"],
        sport=sport,
        home_team=fixture["home_team"],
        away_team=fixture["away_team"],
        league=fixture.get("league", ""),
        kickoff_date=(fixture.get("kickoff_time") or "")[:10],
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
        favorite_win_market_odds=odds_raw.get("match_winner", {}).get("favorite", 1.0),
        over_goals_odds=odds_raw.get("over_goals", {}),
        btts_yes_odds=odds_raw.get("btts_yes"),
        over_cards_odds=odds_raw.get("over_cards", {}),
        market_implied_probabilities=dict(odds_raw.get("market_implied_probabilities", {})),
        data_availability=data_availability or {},
        market_odds_bookmaker_count=odds_raw.get("bookmaker_count"),
    )


def normalize_to_match_snapshot(match_id: int, live_raw: dict) -> MatchSnapshot:
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


LEAGUE_AVERAGE_GOALS_PER_TEAM = 1.3
SHRINKAGE_PSEUDO_GAMES = 5
RECENCY_BLEND_WEIGHT = 0.6

OPEN_METEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

_geocode_cache: dict[str, Optional[tuple]] = {}


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
        target_hour = kickoff_iso[:13]
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
    DAMPEN_PER_PLAYER = 0.03
    MAX_TOTAL_DAMPEN = 0.20
    return max(1.0 - injury_count * DAMPEN_PER_PLAYER, 1.0 - MAX_TOTAL_DAMPEN)


def rest_days_adjustment_factor(days_since_last_match: Optional[int]) -> float:
    if days_since_last_match is None:
        return 1.0
    if days_since_last_match <= 2:
        return 0.93
    if days_since_last_match <= 3:
        return 0.96
    return 1.0


def motivation_adjustment_factor(is_dead_rubber: bool) -> float:
    return 0.90 if is_dead_rubber else 1.0


def adapt_injuries(injuries_raw: list[dict], team_name: str) -> int:
    return sum(1 for inj in injuries_raw if inj.get("team", {}).get("name") == team_name)


def adapt_rest_days(recent_fixtures: list[dict], kickoff_iso: str) -> Optional[int]:
    if not recent_fixtures:
        return None
    try:
        last_match_date = datetime.fromisoformat(recent_fixtures[0]["fixture"]["date"].replace("Z", "+00:00"))
        kickoff_date = datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00"))
        return max((kickoff_date - last_match_date).days, 0)
    except (KeyError, ValueError, TypeError, IndexError):
        return None


def adapt_standings_for_motivation(
    standings, team_name, relegation_spots=3, european_spots=6,
    games_remaining_threshold=5, safety_margin_points=12,
) -> bool:
    if not standings or len(standings) < relegation_spots + european_spots + 1:
        return False
    team_row = next((row for row in standings if row.get("team", {}).get("name") == team_name), None)
    if not team_row:
        return False
    total_teams = len(standings)
    total_games = (total_teams - 1) * 2
    played = team_row.get("all", {}).get("played", 0)
    if total_games - played > games_remaining_threshold:
        return False
    sorted_standings = sorted(standings, key=lambda r: r.get("rank", 999))
    relegation_cutoff_points = sorted_standings[total_teams - relegation_spots - 1].get("points", 0)
    european_cutoff_points = sorted_standings[european_spots - 1].get("points", 0)
    team_points = team_row.get("points", 0)
    safely_clear_of_relegation = team_points - relegation_cutoff_points >= safety_margin_points
    hopelessly_behind_europe = european_cutoff_points - team_points >= safety_margin_points
    return safely_clear_of_relegation and hopelessly_behind_europe


def _estimate_expected_goals(team_stats, is_home, recency_weighted_avg=None, adjustment_factor=1.0) -> float:
    avg_goals_scored = team_stats.get("avg_goals_scored_last_10", 1.2)
    games_played = team_stats.get("games_played", 0)
    shrunk_avg = (
        games_played * avg_goals_scored + SHRINKAGE_PSEUDO_GAMES * LEAGUE_AVERAGE_GOALS_PER_TEAM
    ) / (games_played + SHRINKAGE_PSEUDO_GAMES)
    if recency_weighted_avg is not None:
        shrunk_avg = RECENCY_BLEND_WEIGHT * recency_weighted_avg + (1 - RECENCY_BLEND_WEIGHT) * shrunk_avg
    home_advantage_factor = 1.10 if is_home else 0.92
    return round(shrunk_avg * home_advantage_factor * adjustment_factor, 2)


def adapt_recent_form_goals(fixtures, team_id, venue=None):
    MIN_VENUE_SPLIT_SAMPLES = 2
    goals = []
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
    goals = list(reversed(goals))
    weights = list(range(1, len(goals) + 1))
    weighted_sum = sum(g * w for g, w in zip(goals, weights))
    return round(weighted_sum / sum(weights), 2)


def _estimate_expected_cards(home_stats, away_stats) -> float:
    home_avg = home_stats.get("avg_cards_last_10", 2.0)
    away_avg = away_stats.get("avg_cards_last_10", 2.0)
    return round(home_avg + away_avg, 2)


def _current_season_string(hyphenated: bool = True) -> str:
    today = date.today()
    if not hyphenated:
        return str(today.year)
    if today.month >= 8:
        return f"{today.year}-{today.year + 1}"
    return f"{today.year - 1}-{today.year}"


class APISportsDirectProvider(SportsDataProvider):
    def __init__(self, sport_path, api_key=None, cache_ttl_seconds=300):
        self.sport_path = sport_path
        self.api_key = api_key or os.environ.get("APISPORTS_KEY", "")
        if not self.api_key:
            raise RuntimeError("Chybí APISPORTS_KEY.")
        self.base_url = f"https://v1.{sport_path}.api-sports.io"
        self._cache = InMemoryCache(ttl_seconds=cache_ttl_seconds)

    def _get(self, path, params):
        resp = requests.get(
            f"{self.base_url}{path}", headers={"x-apisports-key": self.api_key},
            params=params, timeout=8,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"API-{self.sport_path}: {payload['errors']}")
        return payload.get("response", [])

    def get_upcoming_matches(self, sport, days_ahead):
        cache_key = f"upcoming:{days_ahead}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        games = []
        today = date.today()
        for offset in range(days_ahead + 1):
            day = today + timedelta(days=offset)
            games.extend(self._get("/games", {"date": day.isoformat()}))
            time.sleep(0.3)
        self._cache.set(cache_key, games)
        return games

    def get_team_statistics(self, sport, team_id):
        cache_key = f"team_stats:{team_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        season = _current_season_string(hyphenated=True)
        response = self._get("/teams/statistics", {"team": team_id, "season": season})
        data = response if isinstance(response, dict) else (response[0] if response else {})
        self._cache.set(cache_key, data)
        return data

    def get_pre_match_odds(self, match_id):
        response = self._get("/odds", {"game": match_id})
        return response[0] if response else {}

    def get_live_match_stats(self, match_id):
        stats = self._get("/games/statistics/teams", {"id": match_id})
        return {"statistics": stats}


def adapt_apisports_game(game):
    return {
        "id": game.get("id"),
        "home_team": game["teams"]["home"]["name"],
        "away_team": game["teams"]["away"]["name"],
        "home_team_id": game["teams"]["home"]["id"],
        "away_team_id": game["teams"]["away"]["id"],
    }


def adapt_apisports_basketball_team_stats(stats):
    points_avg = stats.get("points", {}).get("for", {}).get("average", {}).get("all", "105.0")
    threes_avg = stats.get("threepoint_goals", {}).get("for", {}).get("average", {}).get("all", "12.0")
    return {
        "points_avg": float(points_avg or 105.0),
        "threes_avg": float(threes_avg or 12.0),
    }


def adapt_apisports_hockey_team_stats(stats):
    goals_avg = stats.get("goals", {}).get("for", {}).get("average", {}).get("all", "3.0")
    return {
        "goals_avg": float(goals_avg or 3.0),
        "penalty_minutes_avg": 6.0,
    }


class APITennisProvider(SportsDataProvider):
    BASE_URL = "https://api.api-tennis.com/tennis/"

    def __init__(self, api_key=None, cache_ttl_seconds=300):
        self.api_key = api_key or os.environ.get("APITENNIS_KEY", "")
        if not self.api_key:
            raise RuntimeError("Chybí APITENNIS_KEY.")
        self._cache = InMemoryCache(ttl_seconds=cache_ttl_seconds)

    def _get(self, method, params):
        query = {"method": method, "APIkey": self.api_key, **params}
        resp = requests.get(self.BASE_URL, params=query, timeout=8)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("success") != 1:
            raise RuntimeError(f"api-tennis.com chyba: {payload}")
        return payload.get("result", [])

    def get_upcoming_matches(self, sport, days_ahead):
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

    def get_team_statistics(self, sport, team_id):
        cache_key = f"player_stats:{team_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        response = self._get("get_players", {"player_key": team_id})
        data = response[0] if response else {}
        self._cache.set(cache_key, data)
        return data

    def get_pre_match_odds(self, match_id):
        response = self._get("get_odds", {"match_key": match_id})
        return response[0] if response else {}

    def get_live_match_stats(self, match_id):
        response = self._get("get_livescore", {"match_key": match_id})
        return response[0] if response else {}


def adapt_api_tennis_fixture(fixture):
    return {
        "id": fixture["event_key"],
        "home_team": fixture["event_first_player"],
        "away_team": fixture["event_second_player"],
        "home_team_id": fixture["first_player_key"],
        "away_team_id": fixture["second_player_key"],
    }


def adapt_api_tennis_player_stats(player):
    stats_list = player.get("stats", [])
    current = stats_list[0] if stats_list else {}
    won = int(current.get("matches_won") or 0)
    lost = int(current.get("matches_lost") or 0)
    win_rate = won / (won + lost) if (won + lost) > 0 else 0.5
    return {"win_rate": win_rate}


# =======================================================================
# ODDS API — FIX: rozšířený seznam lig (původně jen EPL + UCL)
# =======================================================================
class OddsAPIProvider:
    BASE_URL = "https://api.the-odds-api.com/v4"

    SPORT_KEYS: dict[Sport, list[str]] = {
        Sport.FOOTBALL: [
            # Evropa
            "soccer_epl",
            "soccer_england_league1",
            "soccer_germany_bundesliga",
            "soccer_germany_bundesliga2",
            "soccer_spain_la_liga",
            "soccer_spain_segunda_division",
            "soccer_italy_serie_a",
            "soccer_italy_serie_b",
            "soccer_france_ligue_one",
            "soccer_france_ligue_two",
            "soccer_netherlands_eredivisie",
            "soccer_portugal_primeira_liga",
            "soccer_turkey_super_league",
            "soccer_belgium_first_div",
            "soccer_greece_super_league",
            "soccer_austria_bundesliga",
            "soccer_czech_republic_liga",
            "soccer_poland_ekstraklasa",
            "soccer_sweden_allsvenskan",
            "soccer_norway_eliteserien",
            "soccer_denmark_superliga",
            "soccer_switzerland_superleague",
            # Evropské poháry
            "soccer_uefa_champs_league",
            "soccer_uefa_europa_league",
            "soccer_uefa_europa_conference_league",
            # Amerika
            "soccer_usa_mls",
            "soccer_usa_usl_league_one",
            "soccer_usa_usl_championship",
            "soccer_mexico_ligamx",
            "soccer_brazil_campeonato",
            "soccer_argentina_primera_division",
        ],
        Sport.BASKETBALL: [
            "basketball_nba",
            "basketball_euroleague",
        ],
        Sport.HOCKEY: [
            "icehockey_nhl",
            "icehockey_sweden_hockey_league",
            "icehockey_czech_extraliga",
        ],
        Sport.TENNIS: [],
    }

    def __init__(self, api_key=None, cache_ttl_seconds=300):
        self.api_key = api_key or os.environ.get("ODDSAPI_KEY", "")
        if not self.api_key:
            raise RuntimeError("Chybí ODDSAPI_KEY.")
        self._cache = InMemoryCache(ttl_seconds=cache_ttl_seconds)

    def get_odds(self, sport: Sport, markets: str = "h2h,totals", regions: str = "eu") -> list[dict]:
        events: list[dict] = []
        for sport_key in self.SPORT_KEYS.get(sport, []):
            cache_key = f"odds:{sport_key}:{markets}"
            cached = self._cache.get(cache_key)
            if cached is not None:
                events.extend(cached)
                continue
            try:
                resp = requests.get(
                    f"{self.BASE_URL}/sports/{sport_key}/odds",
                    params={"apiKey": self.api_key, "regions": regions, "markets": markets, "oddsFormat": "decimal"},
                    timeout=8,
                )
                resp.raise_for_status()
                data = resp.json()
                self._cache.set(cache_key, data)
                events.extend(data)
            except Exception as e:
                print(f"[OddsAPI] chyba pro {sport_key}: {e}")
                continue
        return events


def adapt_odds_api_event(event: dict) -> dict:
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
    totals_by_threshold: dict[float, list[tuple[float, float]]] = {}

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
        threshold = max(totals_by_threshold, key=lambda t: len(totals_by_threshold[t]))
        prices_and_probs = totals_by_threshold[threshold]
        result["over_threshold"] = threshold
        result["over_odds"] = _median([p for p, _ in prices_and_probs])
        result["over_probability"] = sum(p for _, p in prices_and_probs) / len(prices_and_probs)

    return result


import requests
from datetime import date, datetime, timedelta

API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"
MAX_FIXTURES_PER_REQUEST = 40
SINGLE_CALENDAR_YEAR_COMPETITIONS: set[int] = {1}


def _season_year_for_league(league_id: int, today: date) -> int:
    if league_id in SINGLE_CALENDAR_YEAR_COMPETITIONS:
        return today.year
    return today.year if today.month >= 7 else today.year - 1


class _RateLimiter:
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


_api_football_rate_limiter = _RateLimiter(max_per_second=4.0)


class APIFootballProvider(SportsDataProvider):
    def __init__(self, api_key=None, cache_ttl_seconds=300):
        self.api_key = api_key or os.environ.get("APISPORTS_KEY", "")
        if not self.api_key:
            raise RuntimeError("Chybí APISPORTS_KEY.")
        self._cache = InMemoryCache(ttl_seconds=cache_ttl_seconds)

    def _headers(self):
        return {"x-apisports-key": self.api_key}

    def _get(self, path, params):
        _api_football_rate_limiter.wait()
        resp = requests.get(f"{API_FOOTBALL_BASE_URL}{path}", headers=self._headers(), params=params, timeout=8)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"API-Football chyba: {payload['errors']}")
        return payload.get("response", [])

    def get_upcoming_matches(self, sport, days_ahead):
        if sport != Sport.FOOTBALL:
            raise NotImplementedError("APIFootballProvider jen fotbal.")
        cache_key = f"upcoming:{days_ahead}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Načti filtr lig z env proměnné
        league_ids_raw = os.environ.get("FOOTBALL_LEAGUE_IDS", "")
        allowed_league_ids = set()
        if league_ids_raw:
            try:
                allowed_league_ids = {int(x.strip()) for x in league_ids_raw.split(",") if x.strip()}
            except ValueError:
                pass

        per_day_limit = max(MAX_FIXTURES_PER_REQUEST // (days_ahead + 1), 1)
        fixtures = []
        today = date.today()
        for offset in range(days_ahead + 1):
            day = today + timedelta(days=offset)
            day_fixtures = self._get("/fixtures", {"date": day.isoformat()})
            day_fixtures.sort(key=lambda f: f.get("fixture", {}).get("date", ""))
            # Filtruj podle allowed_league_ids pokud je nastaveno
            if allowed_league_ids:
                day_fixtures = [
                    f for f in day_fixtures
                    if f.get("league", {}).get("id") in allowed_league_ids
                ]
            fixtures.extend(day_fixtures[:per_day_limit])
            time.sleep(0.3)

        fixtures = fixtures[:MAX_FIXTURES_PER_REQUEST]

        # Vyfiltruj jen budoucí zápasy (status: scheduled/not started)
        fixtures = [
            f for f in fixtures
            if f.get("fixture", {}).get("status", {}).get("short") in ("NS", "TBD", "PST")
        ]

        print(f"[APIFootball] {len(fixtures)} budoucích zápasů po filtrování")
        self._cache.set(cache_key, fixtures)
        return fixtures

    def get_team_statistics(self, sport, team_id, league_id=None):
        cache_key = f"team_stats:{team_id}:{league_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        if not league_id:
            return {}
        season = _season_year_for_league(int(league_id), date.today())
        response = self._get("/teams/statistics", {"team": team_id, "season": season, "league": league_id})
        data = response if isinstance(response, dict) else (response[0] if response else {})
        self._cache.set(cache_key, data)
        return data

    def get_recent_form(self, team_id, last=5):
        cache_key = f"recent_form:{team_id}:{last}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        fixtures = self._get("/fixtures", {"team": team_id, "last": last, "status": "FT"})
        self._cache.set(cache_key, fixtures)
        return fixtures

    def get_pre_match_odds(self, match_id):
        response = self._get("/odds", {"fixture": match_id})
        return response[0] if response else {}

    def get_live_match_stats(self, match_id):
        stats = self._get("/fixtures/statistics", {"fixture": match_id})
        events = self._get("/fixtures/events", {"fixture": match_id})
        return {"statistics": stats, "events": events}

    def get_live_fixtures(self):
        return self._get("/fixtures", {"live": "all"})

    def get_live_odds(self, match_id):
        response = self._get("/odds/live", {"fixture": match_id})
        return response[0] if response else {}

    def get_fixture_result(self, match_id):
        response = self._get("/fixtures", {"id": match_id})
        return response[0] if response else {}

    def get_injuries(self, match_id):
        return self._get("/injuries", {"fixture": match_id})

    def get_standings(self, league_id, season):
        response = self._get("/standings", {"league": league_id, "season": season})
        try:
            return response[0]["league"]["standings"][0]
        except (IndexError, KeyError, TypeError):
            return []


def adapt_fixture_result(fixture: dict) -> dict:
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
    return {
        "id": fixture["fixture"]["id"],
        "home_team": fixture["teams"]["home"]["name"],
        "away_team": fixture["teams"]["away"]["name"],
        "home_team_id": fixture["teams"]["home"]["id"],
        "away_team_id": fixture["teams"]["away"]["id"],
        "referee": fixture["fixture"].get("referee"),
        "league": fixture.get("league", {}).get("name", ""),
        "league_id": fixture.get("league", {}).get("id"),
        "season": fixture.get("league", {}).get("season"),
        "venue_city": fixture["fixture"].get("venue", {}).get("city"),
        "kickoff_time": fixture["fixture"].get("date"),
    }


def adapt_api_football_team_stats(stats: dict) -> dict:
    goals_avg = (
        stats.get("goals", {}).get("for", {}).get("average", {}).get("total", "1.2") or "1.2"
    )
    yellow_cards = stats.get("cards", {}).get("yellow", {})
    total_yellow = sum(int(v.get("total") or 0) for v in yellow_cards.values() if isinstance(v, dict))
    played = stats.get("fixtures", {}).get("played", {}).get("total") or 1
    return {
        "avg_goals_scored_last_10": float(goals_avg),
        "avg_cards_last_10": round(total_yellow / played, 2),
        "games_played": played,
    }


def is_live_market_blocked(odds_response: dict) -> bool:
    return bool(odds_response.get("blocked", False) or odds_response.get("stopped", False))


def adapt_live_odds_for_signal(odds_response: dict, team_side: str, bookmaker_name: str = "Bet365") -> Optional[float]:
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


def _median(values: list) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    result = s[mid] if n % 2 == 1 else (s[mid - 1] + s[mid]) / 2
    return round(result, 2)


def adapt_api_football_odds(odds_response: dict) -> dict:
    result = {
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
            elif name == "Both Teams Score":
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
        # Jen prahy 1.5 a 2.5 — jediné co Tipsport standardně nabízí
        if threshold not in [1.5, 2.5]:
            continue
        result["over_goals"][threshold] = _median(prices)
        if threshold in under_goals_prices:
            p_over, _ = devig_two_way(_median(prices), _median(under_goals_prices[threshold]))
            result["market_implied_probabilities"][f"over_goals:over_{threshold}"] = p_over
    for threshold, prices in over_cards_prices.items():
        # Jen prahy 1.5 a 2.5 karet
        if threshold not in [1.5, 2.5]:
            continue
        result["over_cards"][threshold] = _median(prices)
    if btts_yes_prices:
        result["btts_yes"] = _median(btts_yes_prices)
        if btts_no_prices:
            p_yes, _ = devig_two_way(_median(btts_yes_prices), _median(btts_no_prices))
            result["market_implied_probabilities"]["btts:yes"] = p_yes

    return result


def adapt_api_football_live_stats(minute: int, live_raw: dict) -> dict:
    stats_by_team: dict[str, dict] = {}
    for team_block in live_raw.get("statistics", []):
        team_name = team_block["team"]["name"]
        stats_by_team[team_name] = {item["type"]: item["value"] for item in team_block.get("statistics", [])}

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


def get_provider(sport: Sport) -> SportsDataProvider:
    if sport == Sport.FOOTBALL:
        return APIFootballProvider()
    if sport == Sport.BASKETBALL:
        return APISportsDirectProvider(sport_path="basketball")
    if sport == Sport.HOCKEY:
        return APISportsDirectProvider(sport_path="hockey")
    if sport == Sport.TENNIS:
        return APITennisProvider()
    raise NotImplementedError(f"Pro sport '{sport.value}' chybí provider.")
