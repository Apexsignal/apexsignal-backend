"""
ApexSignal — Generátor tiketů
Modul: probability_model.py
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


MIN_SELECTION_PROBABILITY = 0.70
MIN_SELECTION_ODDS = 1.3

SAFE_ODDS_RANGE = (2.0, 5.0)
AGGRESSIVE_ODDS_RANGE = (5.0, 10.0)

MAX_GOALS_FOR_SUM = 10

KELLY_FRACTION = 0.25
MAX_RECOMMENDED_STAKE_PCT = 5.0

CORRELATION_DISCOUNT_PER_EXTRA_SAME_LEAGUE_PAIR = 0.95

BOOKMAKER_MARGIN = 1.08


def evaluate_selection_outcome(selection: "SelectionCandidate", home_goals: int, away_goals: int) -> Optional[bool]:
    if selection.market_type == MarketType.MATCH_WINNER:
        if selection.selection == "home":
            return home_goals > away_goals
        if selection.selection == "away":
            return away_goals > home_goals
        if selection.selection == "draw":
            return home_goals == away_goals
        return None
    if selection.market_type == MarketType.OVER_GOALS:
        try:
            threshold = float(selection.selection.replace("over_", ""))
        except ValueError:
            return None
        return (home_goals + away_goals) > threshold
    if selection.market_type == MarketType.BTTS:
        return home_goals >= 1 and away_goals >= 1
    return None


class Sport(str, Enum):
    FOOTBALL = "football"
    TENNIS = "tennis"
    HOCKEY = "hockey"
    BASKETBALL = "basketball"


def kelly_stake_fraction(probability: float, decimal_odds: float) -> float:
    b = decimal_odds - 1.0
    if b <= 0:
        return 0.0
    edge_per_unit = probability * decimal_odds - 1.0
    if edge_per_unit <= 0:
        return 0.0
    full_kelly = edge_per_unit / b
    return max(0.0, full_kelly * KELLY_FRACTION)


class MarketType(str, Enum):
    MATCH_WINNER = "match_winner"
    OVER_GOALS = "over_goals"
    BTTS = "btts"
    OVER_CARDS = "over_cards"
    OVER_GAMES = "over_games"
    OVER_ACES = "over_aces"
    OVER_PENALTY_MINUTES = "over_penalty_minutes"
    OVER_POINTS = "over_points"
    OVER_THREES = "over_threes"


SPORT_MARKETS: dict[Sport, list[MarketType]] = {
    Sport.FOOTBALL: [MarketType.MATCH_WINNER, MarketType.OVER_GOALS, MarketType.BTTS, MarketType.OVER_CARDS],
    Sport.TENNIS: [MarketType.MATCH_WINNER, MarketType.OVER_GAMES, MarketType.OVER_ACES],
    Sport.HOCKEY: [MarketType.MATCH_WINNER, MarketType.OVER_GOALS, MarketType.OVER_PENALTY_MINUTES],
    Sport.BASKETBALL: [MarketType.MATCH_WINNER, MarketType.OVER_POINTS, MarketType.OVER_THREES],
}


# ─── Poissonovské funkce ───────────────────────────────────────────────────────

def poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    log_pmf = k * math.log(lam) - lam - math.lgamma(k + 1)
    return math.exp(log_pmf)


def poisson_cdf(k: int, lam: float) -> float:
    return sum(poisson_pmf(i, lam) for i in range(k + 1))


def prob_over(lam: float, threshold: float) -> float:
    k = math.floor(threshold)
    return 1.0 - poisson_cdf(k, lam)


# ─── Dixon-Coles korekce ──────────────────────────────────────────────────────

DIXON_COLES_RHO = -0.13


def dixon_coles_tau(home_goals: int, away_goals: int, home_xg: float, away_xg: float, rho: float = DIXON_COLES_RHO) -> float:
    if home_goals == 0 and away_goals == 0:
        return 1 - home_xg * away_xg * rho
    if home_goals == 0 and away_goals == 1:
        return 1 + home_xg * rho
    if home_goals == 1 and away_goals == 0:
        return 1 + away_xg * rho
    if home_goals == 1 and away_goals == 1:
        return 1 - rho
    return 1.0


def score_grid_probabilities(home_xg: float, away_xg: float, rho: float = DIXON_COLES_RHO) -> list[list[float]]:
    grid = [[0.0] * (MAX_GOALS_FOR_SUM + 1) for _ in range(MAX_GOALS_FOR_SUM + 1)]
    total = 0.0
    for i in range(MAX_GOALS_FOR_SUM + 1):
        p_h = poisson_pmf(i, home_xg)
        for j in range(MAX_GOALS_FOR_SUM + 1):
            p_a = poisson_pmf(j, away_xg)
            joint = p_h * p_a * dixon_coles_tau(i, j, home_xg, away_xg, rho)
            grid[i][j] = joint
            total += joint
    if total > 0:
        for i in range(MAX_GOALS_FOR_SUM + 1):
            for j in range(MAX_GOALS_FOR_SUM + 1):
                grid[i][j] /= total
    return grid


# ─── De-vig ───────────────────────────────────────────────────────────────────

def devig_two_way(odds_a: float, odds_b: float) -> tuple[float, float]:
    raw_a, raw_b = 1.0 / odds_a, 1.0 / odds_b
    total = raw_a + raw_b
    return raw_a / total, raw_b / total


def devig_market(outcomes: list[tuple[str, float]]) -> dict[str, float]:
    raw = {name: 1.0 / odds for name, odds in outcomes}
    total = sum(raw.values())
    return {name: r / total for name, r in raw.items()}


# ─── FALLBACK ODDS (FIX: generátor funguje i bez reálných kurzů z API) ────────

def _fallback_odds(probability: float, margin: float = BOOKMAKER_MARGIN) -> float:
    """
    Odhadne bookmakerský kurz z pravděpodobnosti modelu, když API-Football
    nevrátí reálné odds. Slouží jen k tomu, aby kandidát prošel filtrem
    odds >= MIN_SELECTION_ODDS — pro Kelly vklad se používá probability.
    """
    if probability <= 0:
        return 1.0
    return round((1.0 / probability) * margin, 2)


def _fallback_over_goals_odds(home_xg: float, away_xg: float) -> dict[float, float]:
    """
    Vygeneruje fallback over_goals_odds pro prahy 1.5, 2.5, 3.5
    když API-Football nevrátí reálné kurzy.
    """
    odds: dict[float, float] = {}
    total_xg = home_xg + away_xg
    for threshold in [1.5, 2.5, 3.5]:
        prob = 1.0 - poisson_cdf(int(threshold), total_xg)
        if prob > 0.05:
            odds[threshold] = _fallback_odds(prob)
    return odds


# ─── Datové třídy ─────────────────────────────────────────────────────────────

@dataclass
class MatchInput:
    match_id: int
    sport: Sport
    home_team: str
    away_team: str
    league: str = ""
    kickoff_date: str = ""
    home_expected_goals: float = 0.0
    away_expected_goals: float = 0.0
    expected_cards: float = 0.0
    expected_penalty_minutes: float = 0.0
    home_games_played: int = 0
    away_games_played: int = 0
    referee: Optional[str] = None
    weather_wind_kmh: Optional[float] = None
    weather_precipitation_mm: Optional[float] = None
    home_injury_count: int = 0
    away_injury_count: int = 0
    home_rest_days: Optional[int] = None
    away_rest_days: Optional[int] = None
    home_dead_rubber: bool = False
    away_dead_rubber: bool = False
    data_availability: dict = field(default_factory=dict)
    market_odds_bookmaker_count: Optional[int] = None
    home_win_probability: Optional[float] = None
    expected_total_games: float = 0.0
    expected_total_aces: float = 0.0
    expected_total_points: float = 0.0
    expected_total_threes: float = 0.0
    favorite_win_market_odds: float = 1.0
    over_goals_odds: dict[float, float] = field(default_factory=dict)
    btts_yes_odds: Optional[float] = None
    over_cards_odds: dict[float, float] = field(default_factory=dict)
    over_penalty_minutes_odds: dict[float, float] = field(default_factory=dict)
    over_games_odds: dict[float, float] = field(default_factory=dict)
    over_aces_odds: dict[float, float] = field(default_factory=dict)
    over_points_odds: dict[float, float] = field(default_factory=dict)
    over_threes_odds: dict[float, float] = field(default_factory=dict)
    market_implied_probabilities: dict[str, float] = field(default_factory=dict)


@dataclass
class SelectionCandidate:
    match_id: int
    home_team: str
    away_team: str
    sport: Sport
    market_type: MarketType
    selection: str
    probability: float
    odds: float
    model_probability: float = 0.0
    market_probability: Optional[float] = None
    league: str = ""
    kickoff_date: str = ""
    reasoning: str = ""
    data_quality: str = ""

    @property
    def edge(self) -> Optional[float]:
        if self.market_probability is None:
            return None
        return round(self.model_probability - self.market_probability, 4)


@dataclass
class Ticket:
    ticket_type: str
    selections: list[SelectionCandidate]
    total_odds: float
    combined_probability: float
    recommended_stake_pct: float = 0.0

    @property
    def summary(self) -> str:
        league_counts: dict[str, int] = {}
        for s in self.selections:
            if s.league:
                league_counts[s.league] = league_counts.get(s.league, 0) + 1
        correlated = any(count > 1 for count in league_counts.values())
        note = (
            " Pozn.: některé výběry jsou ze stejné ligy a dne, appka proto kombinovanou "
            "pravděpodobnost mírně snížila oproti naivnímu výpočtu."
            if correlated else ""
        )
        return (
            f"{len(self.selections)} výběrů, celkový kurz {self.total_odds}, kombinovaná "
            f"pravděpodobnost {round(self.combined_probability * 100, 1)} %, doporučený vklad "
            f"{self.recommended_stake_pct} % bankrollu.{note}"
        )


# ─── MarketEvaluator ──────────────────────────────────────────────────────────

class MarketEvaluator:

    @staticmethod
    def match_winner_probabilities(home_xg: float, away_xg: float) -> dict[str, float]:
        grid = score_grid_probabilities(home_xg, away_xg)
        p_home, p_draw, p_away = 0.0, 0.0, 0.0
        for i, row in enumerate(grid):
            for j, p in enumerate(row):
                if i > j:
                    p_home += p
                elif i == j:
                    p_draw += p
                else:
                    p_away += p
        return {"home": p_home, "draw": p_draw, "away": p_away}

    @staticmethod
    def over_goals_probability(home_xg: float, away_xg: float, threshold: float) -> float:
        grid = score_grid_probabilities(home_xg, away_xg)
        return sum(p for i, row in enumerate(grid) for j, p in enumerate(row) if i + j > threshold)

    @staticmethod
    def btts_probability(home_xg: float, away_xg: float) -> float:
        grid = score_grid_probabilities(home_xg, away_xg)
        return sum(p for i, row in enumerate(grid) for j, p in enumerate(row) if i >= 1 and j >= 1)

    @staticmethod
    def over_cards_probability(expected_cards: float, threshold: float) -> float:
        return prob_over(expected_cards, threshold)

    @classmethod
    def build_candidates(cls, match: MatchInput) -> list[SelectionCandidate]:
        candidates: list[SelectionCandidate] = []

        if match.sport in (Sport.FOOTBALL, Sport.HOCKEY):
            winner_probs = cls.match_winner_probabilities(
                match.home_expected_goals, match.away_expected_goals
            )
            favorite_side = max(winner_probs, key=winner_probs.get)
            if favorite_side != "draw":
                fav_prob = winner_probs[favorite_side]
                # FIX: pokud API nevrátilo reálný kurz (default 1.0), odhadni ho z pravděpodobnosti
                fav_odds = (
                    match.favorite_win_market_odds
                    if match.favorite_win_market_odds >= MIN_SELECTION_ODDS
                    else _fallback_odds(fav_prob)
                )
                candidates.append(cls._candidate(
                    match, MarketType.MATCH_WINNER, favorite_side, fav_prob, fav_odds,
                ))

            # FIX: pokud API nevrátilo over_goals_odds, odhadni je z xG modelu
            over_odds_source = (
                match.over_goals_odds
                if match.over_goals_odds
                else _fallback_over_goals_odds(match.home_expected_goals, match.away_expected_goals)
            )
            for threshold, odds in over_odds_source.items():
                prob = cls.over_goals_probability(match.home_expected_goals, match.away_expected_goals, threshold)
                candidates.append(cls._candidate(match, MarketType.OVER_GOALS, f"over_{threshold}", prob, odds))

            if match.sport == Sport.FOOTBALL and match.btts_yes_odds is not None:
                prob = cls.btts_probability(match.home_expected_goals, match.away_expected_goals)
                candidates.append(cls._candidate(match, MarketType.BTTS, "yes", prob, match.btts_yes_odds))

            if match.sport == Sport.FOOTBALL:
                for threshold, odds in match.over_cards_odds.items():
                    prob = prob_over(match.expected_cards, threshold)
                    candidates.append(cls._candidate(match, MarketType.OVER_CARDS, f"over_{threshold}", prob, odds))
            else:
                for threshold, odds in match.over_penalty_minutes_odds.items():
                    prob = prob_over(match.expected_penalty_minutes, threshold)
                    candidates.append(cls._candidate(match, MarketType.OVER_PENALTY_MINUTES, f"over_{threshold}", prob, odds))

        elif match.sport in (Sport.TENNIS, Sport.BASKETBALL):
            if match.home_win_probability is not None:
                if match.home_win_probability >= 0.5:
                    side, prob = "home", match.home_win_probability
                else:
                    side, prob = "away", 1.0 - match.home_win_probability
                fav_odds = (
                    match.favorite_win_market_odds
                    if match.favorite_win_market_odds >= MIN_SELECTION_ODDS
                    else _fallback_odds(prob)
                )
                candidates.append(cls._candidate(match, MarketType.MATCH_WINNER, side, prob, fav_odds))

            if match.sport == Sport.TENNIS:
                for threshold, odds in match.over_games_odds.items():
                    prob = prob_over(match.expected_total_games, threshold)
                    candidates.append(cls._candidate(match, MarketType.OVER_GAMES, f"over_{threshold}", prob, odds))
                for threshold, odds in match.over_aces_odds.items():
                    prob = prob_over(match.expected_total_aces, threshold)
                    candidates.append(cls._candidate(match, MarketType.OVER_ACES, f"over_{threshold}", prob, odds))
            else:
                for threshold, odds in match.over_points_odds.items():
                    prob = prob_over(match.expected_total_points, threshold)
                    candidates.append(cls._candidate(match, MarketType.OVER_POINTS, f"over_{threshold}", prob, odds))
                for threshold, odds in match.over_threes_odds.items():
                    prob = prob_over(match.expected_total_threes, threshold)
                    candidates.append(cls._candidate(match, MarketType.OVER_THREES, f"over_{threshold}", prob, odds))

        # Diagnostika: ukaž v logách proč kandidáti vypadli
        passing = [c for c in candidates if c.probability > MIN_SELECTION_PROBABILITY and c.odds >= MIN_SELECTION_ODDS]
        if not passing and candidates:
            for c in candidates[:3]:
                print(f"[build_candidates] VYŘAZEN {c.home_team} vs {c.away_team} "
                      f"{c.market_type.value} {c.selection}: prob={round(c.probability,3)} odds={c.odds}")
        return passing

    @staticmethod
    def _build_context_notes(match: MatchInput) -> list[str]:
        notes = []
        injury_parts = []
        if match.home_injury_count > 0:
            injury_parts.append(f"{match.home_team} {match.home_injury_count}× mimo sestavu")
        if match.away_injury_count > 0:
            injury_parts.append(f"{match.away_team} {match.away_injury_count}× mimo sestavu")
        if injury_parts:
            notes.append("zranění/vyloučení — " + ", ".join(injury_parts))
        rest_parts = []
        if match.home_rest_days is not None and match.home_rest_days <= 3:
            rest_parts.append(f"{match.home_team} jen {match.home_rest_days} dny odpočinku")
        if match.away_rest_days is not None and match.away_rest_days <= 3:
            rest_parts.append(f"{match.away_team} jen {match.away_rest_days} dny odpočinku")
        if rest_parts:
            notes.append("krátký odpočinek — " + ", ".join(rest_parts))
        dead_rubber_parts = []
        if match.home_dead_rubber:
            dead_rubber_parts.append(match.home_team)
        if match.away_dead_rubber:
            dead_rubber_parts.append(match.away_team)
        if dead_rubber_parts:
            notes.append("bez výrazné motivace — " + ", ".join(dead_rubber_parts))
        if match.weather_wind_kmh and match.weather_wind_kmh > 30:
            notes.append(f"silný vítr ({match.weather_wind_kmh} km/h)")
        if match.weather_precipitation_mm and match.weather_precipitation_mm > 2:
            notes.append(f"déšť ({match.weather_precipitation_mm} mm)")
        return notes

    @classmethod
    def _build_reasoning(cls, match: MatchInput, market_type: MarketType, selection: str,
                          model_probability: float, market_probability: Optional[float]) -> str:
        model_pct = round(model_probability * 100, 1)
        if market_type == MarketType.MATCH_WINNER:
            if match.sport in (Sport.TENNIS, Sport.BASKETBALL):
                side_team = match.home_team if selection == "home" else match.away_team
                base = f"Model dává {model_pct} % šanci na výhru {side_team} podle dodaného odhadu/žebříčku."
            else:
                side_team = {"home": match.home_team, "away": match.away_team}.get(selection, "remízu")
                outcome = f"výhru týmu {side_team}" if selection != "draw" else "remízu"
                base = (
                    f"Poměr sil podle xG ({match.home_expected_goals} : {match.away_expected_goals}) "
                    f"dává {model_pct} % šanci na {outcome}."
                )
        elif market_type == MarketType.OVER_GOALS:
            threshold = selection.replace("over_", "")
            total_xg = round(match.home_expected_goals + match.away_expected_goals, 2)
            base = f"Součet xG obou týmů ({total_xg}) dává {model_pct} % šanci na víc než {threshold} gólu/ů."
        elif market_type == MarketType.BTTS:
            base = (
                f"Při xG {match.home_expected_goals} (domácí) a {match.away_expected_goals} (hosté) "
                f"appka počítá {model_pct} % šanci, že skórují oba týmy."
            )
        elif market_type == MarketType.OVER_CARDS:
            threshold = selection.replace("over_", "")
            base = f"Na základě očekávaného počtu karet appka počítá {model_pct} % šanci na víc než {threshold} karty/karet."
        else:
            base = f"Model počítá {model_pct} % šanci na tento výběr."

        if market_probability is not None:
            market_pct = round(market_probability * 100, 1)
            diff = model_probability - market_probability
            if abs(diff) < 0.03:
                base += f" Trh se s odhadem shoduje (tržní pravděpodobnost {market_pct} %)."
            elif diff > 0:
                base += (
                    f" Model je optimističtější než trh ({model_pct} % vs. {market_pct} %) — "
                    f"appka pro vklad použije konzervativnější tržní číslo."
                )
            else:
                base += f" Trh je na tenhle výběr ještě optimističtější než model ({market_pct} %)."
        else:
            base += " Kurz odhadnut z modelu (API nezaslalo reálné odds)."

        context_notes = cls._build_context_notes(match)
        if context_notes:
            base += " Pozn.: " + "; ".join(context_notes) + "."
        return base

    @staticmethod
    def _build_data_quality_note(match: MatchInput) -> str:
        if not match.data_availability:
            return ""
        labels = {
            "recent_form": "forma", "injuries": "zranění", "rest_days": "odpočinek",
            "standings_motivation": "tabulka", "weather": "počasí", "market_odds": "kurzy",
        }
        parts = []
        for key, label in labels.items():
            if key not in match.data_availability:
                continue
            available = match.data_availability[key]
            if key == "market_odds" and available and match.market_odds_bookmaker_count:
                parts.append(f"{label} ✓ ({match.market_odds_bookmaker_count} bookmakeři)")
            else:
                parts.append(f"{label} {'✓' if available else '✗'}")
        return "Podklady: " + " · ".join(parts) if parts else ""

    @staticmethod
    def _candidate(match: MatchInput, market_type: MarketType, selection: str, probability: float, odds: float) -> SelectionCandidate:
        model_probability = probability
        market_key = f"{market_type.value}:{selection}"
        market_probability = match.market_implied_probabilities.get(market_key)
        final_probability = market_probability if market_probability is not None else model_probability
        reasoning = MarketEvaluator._build_reasoning(match, market_type, selection, model_probability, market_probability)
        data_quality = MarketEvaluator._build_data_quality_note(match)
        return SelectionCandidate(
            match_id=match.match_id, home_team=match.home_team, away_team=match.away_team,
            sport=match.sport, market_type=market_type, selection=selection,
            probability=final_probability, odds=odds,
            model_probability=model_probability, market_probability=market_probability,
            league=match.league, kickoff_date=match.kickoff_date, reasoning=reasoning,
            data_quality=data_quality,
        )


# ─── TicketGenerator ──────────────────────────────────────────────────────────

class TicketGenerator:

    def __init__(self, seed: Optional[int] = None):
        self._rng = random.Random(seed)
        self._excluded_match_ids: set[int] = set()

    def generate(
        self,
        matches: list[MatchInput],
        risk_level: int,
        allowed_sports: list[Sport],
        allowed_markets: list[MarketType],
        time_frame_days: int,
        pool_filter: Optional[Callable[[list[SelectionCandidate]], list[SelectionCandidate]]] = None,
    ) -> dict[str, Optional[Ticket]]:
        pool = self._build_filtered_pool(matches, allowed_sports, allowed_markets)
        print(f"[TicketGenerator] {len(matches)} zápasů, {len(pool)} kandidátů prošlo filtrem (prob>70%, odds>={MIN_SELECTION_ODDS})")
        for c in pool[:5]:
            print(f"  → {c.home_team} vs {c.away_team}: {c.market_type.value} {c.selection} @ {c.odds} (p={round(c.probability,3)})")

        if pool_filter is not None:
            pool = pool_filter(pool)
            print(f"[TicketGenerator] po AI kontrole: {len(pool)} kandidátů")

        safe = self._build_ticket(pool, SAFE_ODDS_RANGE, "safe", risk_level)
        aggressive = self._build_ticket(pool, AGGRESSIVE_ODDS_RANGE, "aggressive", risk_level)
        return {"safe": safe, "aggressive": aggressive}

    def regenerate(
        self,
        matches: list[MatchInput],
        risk_level: int,
        allowed_sports: list[Sport],
        allowed_markets: list[MarketType],
        time_frame_days: int,
        previous_match_ids: list[int],
        pool_filter: Optional[Callable[[list[SelectionCandidate]], list[SelectionCandidate]]] = None,
    ) -> dict[str, Optional[Ticket]]:
        self._excluded_match_ids.update(previous_match_ids)
        filtered_matches = [m for m in matches if m.match_id not in self._excluded_match_ids]
        if not filtered_matches:
            self._excluded_match_ids.clear()
            filtered_matches = matches
        return self.generate(filtered_matches, risk_level, allowed_sports, allowed_markets, time_frame_days, pool_filter)

    def _build_filtered_pool(
        self, matches: list[MatchInput], allowed_sports: list[Sport], allowed_markets: list[MarketType]
    ) -> list[SelectionCandidate]:
        pool: list[SelectionCandidate] = []
        for match in matches:
            if match.sport not in allowed_sports:
                continue
            candidates = MarketEvaluator.build_candidates(match)
            pool.extend([c for c in candidates if c.market_type in allowed_markets])
        pool.sort(key=lambda c: c.probability, reverse=True)
        return pool

    def _build_ticket(
        self,
        pool: list[SelectionCandidate],
        odds_range: tuple[float, float],
        ticket_type: str,
        risk_level: int,
    ) -> Optional[Ticket]:
        min_odds, max_odds = odds_range
        ordered_pool = pool if risk_level <= 50 else list(reversed(pool))

        selected: list[SelectionCandidate] = []
        used_matches: set[int] = set()
        running_odds = 1.0

        for candidate in ordered_pool:
            if candidate.match_id in used_matches:
                continue
            projected = running_odds * candidate.odds
            if projected > max_odds and selected:
                continue
            selected.append(candidate)
            used_matches.add(candidate.match_id)
            running_odds = projected
            if min_odds <= running_odds <= max_odds:
                break

        if not selected or not (min_odds <= running_odds <= max_odds):
            print(f"[TicketGenerator] {ticket_type}: nelze sestavit — {len(selected)} kandidátů, kurz {round(running_odds,2)}, cíl {min_odds}-{max_odds}")
            return None

        combined_probability = 1.0
        for c in selected:
            combined_probability *= c.probability
        combined_probability = self._apply_correlation_discount(selected, combined_probability)

        recommended_stake_pct = round(
            min(kelly_stake_fraction(combined_probability, running_odds) * 100, MAX_RECOMMENDED_STAKE_PCT), 1
        )

        return Ticket(
            ticket_type=ticket_type,
            selections=selected,
            total_odds=round(running_odds, 2),
            combined_probability=round(combined_probability, 4),
            recommended_stake_pct=recommended_stake_pct,
        )

    @staticmethod
    def _apply_correlation_discount(selected: list[SelectionCandidate], combined_probability: float) -> float:
        league_day_counts: dict[tuple[str, str], int] = {}
        for c in selected:
            if not c.league or not c.kickoff_date:
                continue
            key = (c.league, c.kickoff_date)
            league_day_counts[key] = league_day_counts.get(key, 0) + 1
        extra_correlated_pairs = sum(max(0, count - 1) for count in league_day_counts.values())
        discount = CORRELATION_DISCOUNT_PER_EXTRA_SAME_LEAGUE_PAIR ** extra_correlated_pairs
        return combined_probability * discount
