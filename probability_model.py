"""
ApexSignal — Generátor tiketů
Modul: probability_model.py

Obsahuje:
    - MarketEvaluator: Poissonovský model pro výpočet pravděpodobnosti
      výsledku (výhra/remíza/prohra), over/under gólů a karet.
    - TicketGenerator: sestavuje kombinované tikety ('Safe' kurz 2-5,
      'Aggressive' kurz 5-10) ze vstupního poolu zápasů, striktně
      prioritizuje selekce s pravděpodobností > 70 %.

Vstupní expected_goals / expected_cards (lambda parametry Poissonova
rozdělení) v reálném nasazení dodává `data_provider.py` na základě
historických statistik týmů (xG modely, forma, h2h, atd.).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


MIN_SELECTION_PROBABILITY = 0.70  # striktní podmínka ze zadání

SAFE_ODDS_RANGE = (2.0, 5.0)
AGGRESSIVE_ODDS_RANGE = (5.0, 10.0)

MAX_GOALS_FOR_SUM = 10  # horní mez pro sčítání Poissonova rozdělení (dostatečná přesnost)


class Sport(str, Enum):
    FOOTBALL = "football"
    TENNIS = "tennis"
    HOCKEY = "hockey"
    BASKETBALL = "basketball"


class MarketType(str, Enum):
    MATCH_WINNER = "match_winner"
    OVER_GOALS = "over_goals"
    OVER_CARDS = "over_cards"
    OVER_GAMES = "over_games"               # tenis — celkový počet gamů v zápase
    OVER_ACES = "over_aces"                 # tenis — celkový počet es
    OVER_PENALTY_MINUTES = "over_penalty_minutes"  # hokej — trestné minuty
    OVER_POINTS = "over_points"              # basketbal — celkový počet bodů
    OVER_THREES = "over_threes"              # basketbal — celkový počet trojek


# Které trhy dávají u kterého sportu smysl — používá to i frontend (mapování
# nabízených chipů), aby se u tenisu nenabízel "Over gólů" apod.
SPORT_MARKETS: dict[Sport, list[MarketType]] = {
    Sport.FOOTBALL: [MarketType.MATCH_WINNER, MarketType.OVER_GOALS, MarketType.OVER_CARDS],
    Sport.TENNIS: [MarketType.MATCH_WINNER, MarketType.OVER_GAMES, MarketType.OVER_ACES],
    Sport.HOCKEY: [MarketType.MATCH_WINNER, MarketType.OVER_GOALS, MarketType.OVER_PENALTY_MINUTES],
    Sport.BASKETBALL: [MarketType.MATCH_WINNER, MarketType.OVER_POINTS, MarketType.OVER_THREES],
}


# ---------------------------------------------------------------------
# Poissonovské pravděpodobnostní funkce (bez závislosti na scipy)
# ---------------------------------------------------------------------
def poisson_pmf(k: int, lam: float) -> float:
    """
    P(X = k) pro Poissonovo rozdělení s parametrem lam.
    Počítáno v log-prostoru (přes lgamma), aby to nepřeteklo u velkých
    lam/k — třeba u basketbalových bodů (lam ~ 110-220), kde lam**k jako
    přímý float by overflowoval ještě před vydělením faktoriálem.
    """
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    log_pmf = k * math.log(lam) - lam - math.lgamma(k + 1)
    return math.exp(log_pmf)


def poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k)."""
    return sum(poisson_pmf(i, lam) for i in range(k + 1))


def prob_over(lam: float, threshold: float) -> float:
    """
    P(X > threshold) pro 'over' trhy typu 2.5, 4.5 apod.
    threshold je vždy X.5, takže P(X > 2.5) = 1 - P(X <= 2).
    """
    k = math.floor(threshold)
    return 1.0 - poisson_cdf(k, lam)


# ---------------------------------------------------------------------
# De-vig: odstranění bookmakerské marže z kurzů → "fair" pravděpodobnost.
# Tohle je statisticky spolehlivější vstup než vlastní heuristický odhad,
# protože tržní kurz už v sobě zahrnuje obrovské množství informací
# (zranění, počasí, sestavy...), které náš model nemá k dispozici.
# Používá to data_provider.py, když má k zápasu reálné kurzy z the-odds-api.
# ---------------------------------------------------------------------
def devig_two_way(odds_a: float, odds_b: float) -> tuple[float, float]:
    """Dvou-výsledkový trh (např. over/under). Vrací (prob_a, prob_b), které se sčítají na 1.0."""
    raw_a, raw_b = 1.0 / odds_a, 1.0 / odds_b
    total = raw_a + raw_b
    return raw_a / total, raw_b / total


def devig_market(outcomes: list[tuple[str, float]]) -> dict[str, float]:
    """Obecná verze pro N výsledků (např. 1X2 se třemi výsledky: home/draw/away)."""
    raw = {name: 1.0 / odds for name, odds in outcomes}
    total = sum(raw.values())
    return {name: r / total for name, r in raw.items()}


# ---------------------------------------------------------------------
# Vstupní data o zápase (dodává data_provider.py)
# ---------------------------------------------------------------------
@dataclass
class MatchInput:
    match_id: int
    sport: Sport
    home_team: str
    away_team: str
    # Fotbal / hokej (góly modelované Poissonem)
    home_expected_goals: float = 0.0
    away_expected_goals: float = 0.0
    expected_cards: float = 0.0                # fotbal
    expected_penalty_minutes: float = 0.0      # hokej
    # Tenis / basketbal — výhra se NEpočítá z gólů (nedává smysl), ale
    # dodává se přímo jako pravděpodobnost (z žebříčku/Elo modelu nebo
    # z bookmakerského kurzu) přes data_provider.py
    home_win_probability: Optional[float] = None
    expected_total_games: float = 0.0          # tenis — celkový počet gamů
    expected_total_aces: float = 0.0           # tenis — celkový počet es
    expected_total_points: float = 0.0          # basketbal — celkový počet bodů
    expected_total_threes: float = 0.0          # basketbal — celkový počet trojek

    favorite_win_market_odds: float = 1.0
    over_goals_odds: dict[float, float] = field(default_factory=dict)            # {2.5: 1.85, ...}
    over_cards_odds: dict[float, float] = field(default_factory=dict)            # {3.5: 1.90, ...}
    over_penalty_minutes_odds: dict[float, float] = field(default_factory=dict)  # {8.5: 1.90, ...}
    over_games_odds: dict[float, float] = field(default_factory=dict)            # {21.5: 1.85, ...}
    over_aces_odds: dict[float, float] = field(default_factory=dict)             # {8.5: 1.90, ...}
    over_points_odds: dict[float, float] = field(default_factory=dict)           # {225.5: 1.90, ...}
    over_threes_odds: dict[float, float] = field(default_factory=dict)           # {24.5: 1.90, ...}

    # Pokud data_provider.py sežene reálné kurzy z the-odds-api, naplní se
    # sem de-vigované (fair) pravděpodobnosti klíčované "market_type:selection"
    # (např. "match_winner:home", "over_goals:over_2.5") — MarketEvaluator
    # jim dá přednost před vlastním heuristickým odhadem, viz _candidate().
    market_implied_probabilities: dict[str, float] = field(default_factory=dict)


@dataclass
class SelectionCandidate:
    """Jedna konkrétní sázková příležitost po vyhodnocení modelem."""
    match_id: int
    home_team: str
    away_team: str
    sport: Sport
    market_type: MarketType
    selection: str          # 'home' / 'draw' / 'away' / 'over_2.5' / 'over_4.5' ...
    probability: float      # model_probability, musí být > 0.70 pro zařazení do poolu
    odds: float


@dataclass
class Ticket:
    ticket_type: str                 # 'safe' / 'aggressive'
    selections: list[SelectionCandidate]
    total_odds: float
    combined_probability: float


class MarketEvaluator:
    """Vyhodnocuje pravděpodobnosti jednotlivých trhů pro daný zápas."""

    @staticmethod
    def match_winner_probabilities(home_xg: float, away_xg: float) -> dict[str, float]:
        """Vrací P(home), P(draw), P(away) na základě nezávislého Poissonova modelu."""
        p_home, p_draw, p_away = 0.0, 0.0, 0.0
        for hg in range(MAX_GOALS_FOR_SUM + 1):
            p_h = poisson_pmf(hg, home_xg)
            for ag in range(MAX_GOALS_FOR_SUM + 1):
                p_a = poisson_pmf(ag, away_xg)
                joint = p_h * p_a
                if hg > ag:
                    p_home += joint
                elif hg == ag:
                    p_draw += joint
                else:
                    p_away += joint
        return {"home": p_home, "draw": p_draw, "away": p_away}

    @staticmethod
    def over_goals_probability(home_xg: float, away_xg: float, threshold: float) -> float:
        return prob_over(home_xg + away_xg, threshold)

    @staticmethod
    def over_cards_probability(expected_cards: float, threshold: float) -> float:
        return prob_over(expected_cards, threshold)

    @classmethod
    def build_candidates(cls, match: MatchInput) -> list[SelectionCandidate]:
        """
        Vygeneruje kandidáty pro VŠECHNY relevantní trhy daného zápasu —
        které trhy to jsou, závisí na sportu (viz SPORT_MARKETS). Vrátí
        jen ty, jejichž model_probability > MIN_SELECTION_PROBABILITY.
        """
        candidates: list[SelectionCandidate] = []

        if match.sport in (Sport.FOOTBALL, Sport.HOCKEY):
            # Góly modelované Poissonem — pro tyto dva sporty to dává smysl
            winner_probs = cls.match_winner_probabilities(
                match.home_expected_goals, match.away_expected_goals
            )
            favorite_side = max(winner_probs, key=winner_probs.get)
            if favorite_side != "draw":
                candidates.append(cls._candidate(
                    match, MarketType.MATCH_WINNER, favorite_side,
                    winner_probs[favorite_side], match.favorite_win_market_odds,
                ))
            for threshold, odds in match.over_goals_odds.items():
                prob = cls.over_goals_probability(match.home_expected_goals, match.away_expected_goals, threshold)
                candidates.append(cls._candidate(match, MarketType.OVER_GOALS, f"over_{threshold}", prob, odds))

            if match.sport == Sport.FOOTBALL:
                for threshold, odds in match.over_cards_odds.items():
                    prob = prob_over(match.expected_cards, threshold)
                    candidates.append(cls._candidate(match, MarketType.OVER_CARDS, f"over_{threshold}", prob, odds))
            else:  # HOCKEY
                for threshold, odds in match.over_penalty_minutes_odds.items():
                    prob = prob_over(match.expected_penalty_minutes, threshold)
                    candidates.append(cls._candidate(match, MarketType.OVER_PENALTY_MINUTES, f"over_{threshold}", prob, odds))

        elif match.sport in (Sport.TENNIS, Sport.BASKETBALL):
            # Tady góly nedávají smysl — výhra se bere přímo z dodané
            # pravděpodobnosti (žebříček/Elo/bookmaker), ne z Poissonu na góly.
            if match.home_win_probability is not None:
                if match.home_win_probability >= 0.5:
                    side, prob = "home", match.home_win_probability
                else:
                    side, prob = "away", 1.0 - match.home_win_probability
                candidates.append(cls._candidate(match, MarketType.MATCH_WINNER, side, prob, match.favorite_win_market_odds))

            if match.sport == Sport.TENNIS:
                for threshold, odds in match.over_games_odds.items():
                    prob = prob_over(match.expected_total_games, threshold)
                    candidates.append(cls._candidate(match, MarketType.OVER_GAMES, f"over_{threshold}", prob, odds))
                for threshold, odds in match.over_aces_odds.items():
                    prob = prob_over(match.expected_total_aces, threshold)
                    candidates.append(cls._candidate(match, MarketType.OVER_ACES, f"over_{threshold}", prob, odds))
            else:  # BASKETBALL
                for threshold, odds in match.over_points_odds.items():
                    prob = prob_over(match.expected_total_points, threshold)
                    candidates.append(cls._candidate(match, MarketType.OVER_POINTS, f"over_{threshold}", prob, odds))
                for threshold, odds in match.over_threes_odds.items():
                    prob = prob_over(match.expected_total_threes, threshold)
                    candidates.append(cls._candidate(match, MarketType.OVER_THREES, f"over_{threshold}", prob, odds))

        # striktní podmínka ze zadání: jen pravděpodobnost > 70 %
        return [c for c in candidates if c.probability > MIN_SELECTION_PROBABILITY]

    @staticmethod
    def _candidate(match: MatchInput, market_type: MarketType, selection: str, probability: float, odds: float) -> SelectionCandidate:
        # Pokud máme reálnou tržní (de-vigovanou) pravděpodobnost pro tuhle
        # přesnou selekci, použijeme ji místo vlastního heuristického odhadu —
        # je to spolehlivější vstup (viz devig_market výše).
        market_key = f"{market_type.value}:{selection}"
        if market_key in match.market_implied_probabilities:
            probability = match.market_implied_probabilities[market_key]
        return SelectionCandidate(
            match_id=match.match_id, home_team=match.home_team, away_team=match.away_team,
            sport=match.sport, market_type=market_type, selection=selection,
            probability=probability, odds=odds,
        )


class TicketGenerator:
    """
    Sestavuje kombinované tikety z poolu kandidátů (SelectionCandidate),
    které už mají model_probability > 70 % (filtrováno v MarketEvaluator).
    """

    def __init__(self, seed: Optional[int] = None):
        self._rng = random.Random(seed)
        self._excluded_match_ids: set[int] = set()  # pro 'Regenerovat'

    def generate(
        self,
        matches: list[MatchInput],
        risk_level: int,                     # 0-100, posuvník
        allowed_sports: list[Sport],
        allowed_markets: list[MarketType],
        time_frame_days: int,
    ) -> dict[str, Optional[Ticket]]:
        pool = self._build_filtered_pool(matches, allowed_sports, allowed_markets)

        # risk_level ovlivňuje, zda preferujeme méně/větší počet selekcí
        # a zda v Aggressive tiketu sahá model i po slabších (ale stále >70 %) kombinacích
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
    ) -> dict[str, Optional[Ticket]]:
        """AI načte nové zápasy — vyloučí dříve použité a sestaví znovu."""
        self._excluded_match_ids.update(previous_match_ids)
        filtered_matches = [m for m in matches if m.match_id not in self._excluded_match_ids]
        if not filtered_matches:
            self._excluded_match_ids.clear()  # pool vyčerpán, reset
            filtered_matches = matches
        return self.generate(filtered_matches, risk_level, allowed_sports, allowed_markets, time_frame_days)

    # ------------------------------------------------------------------
    def _build_filtered_pool(
        self, matches: list[MatchInput], allowed_sports: list[Sport], allowed_markets: list[MarketType]
    ) -> list[SelectionCandidate]:
        pool: list[SelectionCandidate] = []
        for match in matches:
            if match.sport not in allowed_sports:
                continue
            candidates = MarketEvaluator.build_candidates(match)
            pool.extend([c for c in candidates if c.market_type in allowed_markets])
        # nejlepší (nejjistější) selekce první — řadíme dle pravděpodobnosti
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
        # risk_level > 50 => i v rámci >70% poolu povolí kombinovat slabší (ale stále platné) konce
        ordered_pool = pool if risk_level <= 50 else list(reversed(pool))

        selected: list[SelectionCandidate] = []
        used_matches: set[int] = set()
        running_odds = 1.0

        for candidate in ordered_pool:
            if candidate.match_id in used_matches:
                continue  # jeden zápas max. jednou v rámci tiketu (bez korelovaných duplicit)
            projected = running_odds * candidate.odds
            if projected > max_odds and selected:
                continue  # přesáhli bychom horní hranici, zkusíme jinou kombinaci
            selected.append(candidate)
            used_matches.add(candidate.match_id)
            running_odds = projected
            if min_odds <= running_odds <= max_odds:
                break

        if not selected or not (min_odds <= running_odds <= max_odds):
            return None  # nepodařilo se sestavit tiket v cílovém rozsahu kurzu z dostupných dat

        combined_probability = 1.0
        for c in selected:
            combined_probability *= c.probability

        return Ticket(
            ticket_type=ticket_type,
            selections=selected,
            total_odds=round(running_odds, 2),
            combined_probability=round(combined_probability, 4),
        )
