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
from typing import Callable, Optional


MIN_SELECTION_PROBABILITY = 0.70  # striktní podmínka pro Safe tiket
MIN_SELECTION_PROBABILITY_AGGR = 0.65  # mírně nižší práh pro Aggressive — více výběrů, větší kurz
MIN_SELECTION_ODDS = 1.3  # appka odmítne výběry s kurzem pod touhle hranicí
MAX_SELECTION_ODDS = 5.0  # appka odmítne výběry s kurzem nad touhle hranicí —
                           # kurz 11.0 při pravděpodobnosti 78% je podezřelý
                           # (model vidí jinou hodnotu než trh, nebo kurz chybí
                           # a appka ho špatně odhadla). Max 5.0 je konzervativní
                           # ale realistický strop pro jednotlivý výběr v kombinaci.

SAFE_ODDS_RANGE = (2.0, 5.0)       # zachováno pro zpětnou kompatibilitu
AGGRESSIVE_ODDS_RANGE = (5.0, 10.0)  # zachováno pro zpětnou kompatibilitu

# Tři rozsahy pro nový systém délky tiketu
TICKET_RANGES = {
    "kratky":  (1.8,  3.5),   # Krátký — 2-3 výběry
    "stredni": (2.5,  7.0),   # Střední — 3-5 výběrů
    "dlouhy":  (5.0, 15.0),   # Dlouhý — 5-8 výběrů
}

MAX_GOALS_FOR_SUM = 10  # horní mez pro sčítání Poissonova rozdělení (dostatečná přesnost)

KELLY_FRACTION = 0.25         # appka sází jen čtvrtinu plného Kelly výpočtu jako
                               # rezervu proti tomu, že náš odhad pravděpodobnosti
                               # není perfektní — plný Kelly je při nadhodnoceném
                               # modelu nebezpečně agresivní
MAX_RECOMMENDED_STAKE_PCT = 5.0  # tvrdý strop, i kdyby Kelly počítal víc

CORRELATION_DISCOUNT_PER_EXTRA_SAME_LEAGUE_PAIR = 0.95  # viz _apply_correlation_discount


def evaluate_selection_outcome(selection: "SelectionCandidate", home_goals: int, away_goals: int) -> Optional[bool]:
    """
    Vyhodnotí, jestli se tahle konkrétní selekce podle finálního skóre
    potvrdila (True/False). Appka umí rozhodnout jen trhy odvozené čistě
    ze skóre (MATCH_WINNER, OVER_GOALS) — cokoli jiného (karty, tenisové/
    basketbalové trhy) appka automaticky nevyhodnotí a vrátí None; tiket
    pak zůstane 'pending', dokud ho někdo nevyhodnotí jinak.
    """
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
    """
    Kelly kritérium: jaký podíl bankrollu vsadit, aby dlouhodobě rostl
    nejrychleji bez rizika krachu. b = čistý zisk na jednotku sázky
    (odds - 1), f* = (p*b - (1-p)) / b. Appka používá jen KELLY_FRACTION
    (čtvrtinu) výsledku jako bezpečnostní rezervu — plný Kelly je při jen
    mírně nadhodnoceném modelu nebezpečně agresivní.

    Vrací 0.0, pokud sázka nemá kladnou očekávanou hodnotu (p*odds <= 1) —
    appka v takovém případě nedoporučí vsadit nic, bez ohledu na to, jak
    "jistá" selekce vypadá podle naší vlastní pravděpodobnosti.
    """
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
    BTTS = "btts"                            # fotbal — oba týmy dají gól (Both Teams To Score)
    OVER_CARDS = "over_cards"
    OVER_GAMES = "over_games"               # tenis — celkový počet gamů v zápase
    OVER_ACES = "over_aces"                 # tenis — celkový počet es
    OVER_PENALTY_MINUTES = "over_penalty_minutes"  # hokej — trestné minuty
    OVER_POINTS = "over_points"              # basketbal — celkový počet bodů
    OVER_THREES = "over_threes"              # basketbal — celkový počet trojek


# Které trhy dávají u kterého sportu smysl — používá to i frontend (mapování
# nabízených chipů), aby se u tenisu nenabízel "Over gólů" apod.
SPORT_MARKETS: dict[Sport, list[MarketType]] = {
    Sport.FOOTBALL: [MarketType.MATCH_WINNER, MarketType.OVER_GOALS, MarketType.BTTS, MarketType.OVER_CARDS],
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
# Dixon-Coles korekce (Dixon & Coles, 1997): základní nezávislý Poissonův
# model systematicky podhodnocuje nízkoskórující remízy (0:0, 1:1) a
# nadhodnocuje výsledky 1:0/0:1 — týmy se v těsných zápasech chovají
# opatrněji, než nezávislost gólů předpokládá. Tau koriguje právě tyhle
# čtyři výsledky, ostatní necháva beze změny.
# rho = -0.13 je standardní literaturní odhad (anglická liga, Dixon & Coles
# 1997); jde dál zpřesnit přeurčením zvlášť pro každou ligu z historických
# dat, ale fixní hodnota je solidní vylepšení oproti žádné korekci.
# ---------------------------------------------------------------------
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
    """
    Normalizovaná mřížka P(home_goals=i, away_goals=j), 0..MAX_GOALS_FOR_SUM,
    s vestavěnou Dixon-Coles korekcí. Match winner i over/under na celkový
    počet gólů se odvozují ze STEJNÉ mřížky, aby byly mezi sebou konzistentní.
    """
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
    # Liga/soutěž a den výkopu (jen datum, bez času) — appka to používá
    # k odhadu korelace mezi výběry ve stejném kombo tiketu (viz
    # TicketGenerator._apply_correlation_discount). Bez týmů sdílejících
    # ligu+den appka korelaci nepředpokládá.
    league: str = ""
    country: str = ""        # země soutěže — zobrazuje se v UI u výběru
    league_id: Optional[int] = None  # ID ligy z API-Football
    kickoff_date: str = ""  # ISO formát YYYY-MM-DD
    # Fotbal / hokej (góly modelované Poissonem)
    home_expected_goals: float = 0.0
    away_expected_goals: float = 0.0
    expected_cards: float = 0.0                # fotbal
    expected_penalty_minutes: float = 0.0      # hokej
    # Kolik zápasů má tým v sezóně odehráno — slouží jen jako metadata pro
    # transparentnost (appka sama o sobě se s nízkým vzorkem opatrnější chová
    # už dřív, přes "shrinkage" v data_provider._estimate_expected_goals).
    home_games_played: int = 0
    away_games_played: int = 0
    # Jméno rozhodčího (API-Football ho vrací zadarmo u každého zápasu).
    # Zatím se nepoužívá k úpravě pravděpodobnosti karet — na to chybí
    # historická data (průměr karet per rozhodčí), appka jen jméno zatím nese
    # dál, ať je připravená, až historii začneme sbírat.
    referee: Optional[str] = None
    # Počasí v čase výkopu (Open-Meteo, zdarma) — appka z něj v
    # data_provider._estimate_expected_goals už spočítala mírnou korekci
    # expected goals; tady se nese dál jen jako metadata pro transparentnost
    # (např. budoucí "hraje se za silného deště" badge v UI).
    weather_wind_kmh: Optional[float] = None
    weather_precipitation_mm: Optional[float] = None
    # Appka tyhle hodnoty použije k úpravě xG (viz data_provider.py), ale
    # uchovává si je i samostatně — bez toho by zdůvodnění výběru (viz
    # SelectionCandidate.reasoning) nemělo jak zmínit KONKRÉTNÍ důvod
    # (kolik zranění, kolik dní odpočinku...), jen výsledné upravené číslo.
    home_injury_count: int = 0
    away_injury_count: int = 0
    home_rest_days: Optional[int] = None
    away_rest_days: Optional[int] = None
    home_dead_rubber: bool = False
    away_dead_rubber: bool = False
    # Appka si pamatuje, KTERÉ zdroje dat se reálně podařilo načíst — ne
    # jen výsledné hodnoty (0 zranění může znamenat "žádná zranění" NEBO
    # "appka se k datům nedostala", to bez tohohle nejde rozlišit). Klíče:
    # "recent_form", "injuries", "rest_days", "standings_motivation",
    # "weather", "market_odds". Chybějící klíč = appka to ani nezkoušela
    # (typicky u sportů, kde daný zdroj nedává smysl).
    data_availability: dict = field(default_factory=dict)
    market_odds_bookmaker_count: Optional[int] = None
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
    btts_yes_odds: Optional[float] = None      # kurz na "oba týmy dají gól: ano"
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
    probability: float      # finální pravděpodobnost použitá pro staking — tržní,
                             # pokud je k dispozici (spolehlivější), jinak náš model
    odds: float
    model_probability: float = 0.0       # náš vlastní heuristický odhad, NEZÁVISLE na trhu
    market_probability: Optional[float] = None  # de-vigovaná tržní pravděpodobnost, pokud appka má kurzy
    league: str = ""
    country: str = ""
    league_id: Optional[int] = None
    kickoff_date: str = ""
    reasoning: str = ""   # lidsky čitelné zdůvodnění, proč appka tenhle výběr nabídla
    data_quality: str = ""  # krátký přehled, které zdroje dat appka reálně sehnala

    @property
    def edge(self) -> Optional[float]:
        """
        Rozdíl mezi naším modelem a trhem. Appka pro staking vždy použije
        tržní číslo, pokud existuje (probability výše) — edge je čistě
        diagnostický údaj pro uživatele: velký kladný rozdíl znamená, že náš
        model je výrazně optimističtější než trh, což je důvod k opatrnosti,
        ne k nadšení (model může vidět něco navíc, ale stejně tak může jen
        chybovat / nemít kontext, co trh už zohlednil).
        """
        if self.market_probability is None:
            return None
        return round(self.model_probability - self.market_probability, 4)


@dataclass
class Ticket:
    ticket_type: str                 # 'safe' / 'aggressive'
    selections: list[SelectionCandidate]
    total_odds: float
    combined_probability: float
    recommended_stake_pct: float = 0.0   # % bankrollu, frakční Kelly (viz kelly_stake_fraction)

    @property
    def summary(self) -> str:
        """Krátké shrnutí celého tiketu — kolik výběrů, jaký kurz, a poznámka, pokud appka korigovala kombinovanou pravděpodobnost za korelaci."""
        league_counts: dict[str, int] = {}
        for s in self.selections:
            if s.league:
                league_counts[s.league] = league_counts.get(s.league, 0) + 1
        correlated = any(count > 1 for count in league_counts.values())
        note = (
            " Pozn.: některé výběry jsou ze stejné ligy a dne, appka proto kombinovanou "
            "pravděpodobnost mírně snížila oproti naivnímu výpočtu (viz korelační korekce)."
            if correlated else ""
        )
        return (
            f"{len(self.selections)} výběrů, celkový kurz {self.total_odds}, kombinovaná "
            f"pravděpodobnost {round(self.combined_probability * 100, 1)} %, doporučený vklad "
            f"{self.recommended_stake_pct} % bankrollu.{note}"
        )


class MarketEvaluator:
    """Vyhodnocuje pravděpodobnosti jednotlivých trhů pro daný zápas."""

    @staticmethod
    def match_winner_probabilities(home_xg: float, away_xg: float) -> dict[str, float]:
        """Vrací P(home), P(draw), P(away) na základě Dixon-Coles korigované mřížky."""
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
        """
        P(oba týmy skórují aspoň jednou) ze stejné Dixon-Coles korigované
        mřížky, co používáme pro výhru i over/under gólů — žádný nový
        model, jen jiný způsob, jak se na tu samou mřížku skóre podívat.
        Součet všech buněk i>=1 AND j>=1 (= 1 - P(home=0) - P(away=0) +
        P(0:0), ale jednodušší a méně náchylné na chyby je to sečíst
        přímo z mřížky).
        """
        grid = score_grid_probabilities(home_xg, away_xg)
        return sum(p for i, row in enumerate(grid) for j, p in enumerate(row) if i >= 1 and j >= 1)

    @staticmethod
    def over_cards_probability(expected_cards: float, threshold: float) -> float:
        return prob_over(expected_cards, threshold)

    @classmethod
    def build_candidates(cls, match: MatchInput, min_prob: float = MIN_SELECTION_PROBABILITY) -> list[SelectionCandidate]:
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

            if match.sport == Sport.FOOTBALL and match.btts_yes_odds is not None:
                prob = cls.btts_probability(match.home_expected_goals, match.away_expected_goals)
                candidates.append(cls._candidate(match, MarketType.BTTS, "yes", prob, match.btts_yes_odds))

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

        # Různé minimální kurzy podle typu trhu:
        # - Výhra favorita: min 1.20 (kurz 1.22 při 75% je stále informačně zajímavý)
        # - Over góly/karty: min 1.30 (bez "jistých" tipů za kurz 1.01)
        # Max kurz 5.0 pro všechny (kurz 11.0 při 78% je podezřelý odhad)
        def passes_odds_filter(c: SelectionCandidate) -> bool:
            if c.market_type == MarketType.MATCH_WINNER:
                return 1.20 <= c.odds <= MAX_SELECTION_ODDS
            return MIN_SELECTION_ODDS <= c.odds <= MAX_SELECTION_ODDS

        return [c for c in candidates if c.probability > min_prob and passes_odds_filter(c)]

    @staticmethod
    def _build_context_notes(match: MatchInput) -> list[str]:
        """Krátké poznámky o faktorech, co ovlivnily odhad xG pro tenhle zápas — připojují se na konec zdůvodnění výběru."""
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
            notes.append("bez výrazné motivace (nehraje se o nic) — " + ", ".join(dead_rubber_parts))

        if match.weather_wind_kmh and match.weather_wind_kmh > 30:
            notes.append(f"silný vítr ({match.weather_wind_kmh} km/h)")
        if match.weather_precipitation_mm and match.weather_precipitation_mm > 2:
            notes.append(f"déšť ({match.weather_precipitation_mm} mm)")
        return notes

    @classmethod
    def _build_reasoning(cls, match: MatchInput, market_type: MarketType, selection: str,
                          model_probability: float, market_probability: Optional[float]) -> str:
        """Sestaví lidsky čitelné zdůvodnění výběru — základ podle typu trhu, pak shoda/neshoda s trhem, pak kontextové poznámky."""
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
            base = f"Součet očekávaných gólů obou týmů (xG celkem {total_xg}) dává {model_pct} % šanci na víc než {threshold} gólu/ů."
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
            base += " Appka nemá k dispozici tržní kurz pro nezávislé srovnání, jede čistě na vlastním modelu."

        context_notes = MarketEvaluator._build_context_notes(match)
        if context_notes:
            base += " Pozn.: " + "; ".join(context_notes) + "."

        return base

    @staticmethod
    def _build_data_quality_note(match: MatchInput) -> str:
        """
        Appka tu shrne, KTERÉ zdroje dat se reálně podařilo sehnat — ne
        jejich výsledek, jen jestli appka měla šanci je vůbec zohlednit.
        0 nahlášených zranění může znamenat "tým je v pořádku" i "appka
        se k datům nedostala" — bez tohoto přehledu by uživatel nepoznal
        rozdíl. Appka zobrazuje jen zdroje relevantní pro daný sport
        (u tenisu/basketbalu nedává smysl hlásit "zranění" apod.).
        """
        if not match.data_availability:
            return ""
        labels = {
            "recent_form": "forma",
            "injuries": "zranění",
            "rest_days": "odpočinek",
            "standings_motivation": "tabulka",
            "weather": "počasí",
            "market_odds": "kurzy",
        }
        parts = []
        for key, label in labels.items():
            if key not in match.data_availability:
                continue
            available = match.data_availability[key]
            if key == "market_odds" and available and match.market_odds_bookmaker_count:
                parts.append(f"{label} ✓ ({match.market_odds_bookmaker_count} bookmakeři)")
            else:
                parts.append(f"{label} {'✓' if available else '✗ (nedostupná)'}")
        return "Podklady: " + " · ".join(parts) if parts else ""

    @staticmethod
    def _candidate(match: MatchInput, market_type: MarketType, selection: str, probability: float, odds: float) -> SelectionCandidate:
        # model_probability je VŽDY náš vlastní heuristický odhad, nezávisle
        # na tom, jestli appka má tržní data. Pokud reálnou (de-vigovanou)
        # tržní pravděpodobnost pro tuhle přesnou selekci máme, použijeme ji
        # jako finální 'probability' pro staking — je to spolehlivější vstup
        # (viz devig_market výše) — ale model_probability si appka uchová
        # zvlášť, ať lze spočítat edge (viz SelectionCandidate.edge).
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
            league=match.league, country=match.country, league_id=match.league_id,
            kickoff_date=match.kickoff_date, reasoning=reasoning,
            data_quality=data_quality,
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
        risk_level: int,                     # 20=krátký, 50=střední, 80=dlouhý
        allowed_sports: list[Sport],
        allowed_markets: list[MarketType],
        time_frame_days: int,
        pool_filter: Optional[Callable[[list[SelectionCandidate]], list[SelectionCandidate]]] = None,
    ) -> dict[str, Optional[Ticket]]:
        # Mapování risk_level na délku tiketu
        if risk_level <= 30:
            ticket_key = "kratky"
            min_prob = MIN_SELECTION_PROBABILITY
        elif risk_level <= 60:
            ticket_key = "stredni"
            min_prob = MIN_SELECTION_PROBABILITY
        else:
            ticket_key = "dlouhy"
            min_prob = MIN_SELECTION_PROBABILITY_AGGR  # delší tiket potřebuje víc kandidátů

        odds_range = TICKET_RANGES[ticket_key]

        # Fallback práh — pokud appka nenajde kandidáty při 70%, zkusí 65% a pak 60%.
        # Appka to dělá automaticky a tiché — uživatel dostane tiket s poznámkou
        # že byl použit nižší práh, místo prázdného výsledku.
        FALLBACK_THRESHOLDS = [min_prob, 0.65, 0.60]
        pool = []
        used_threshold = min_prob
        for threshold in FALLBACK_THRESHOLDS:
            pool = self._build_filtered_pool(matches, allowed_sports, allowed_markets, min_prob=threshold)
            used_threshold = threshold
            print(f"[TicketGenerator] {len(matches)} zápasů — {ticket_key} pool: {len(pool)} kandidátů @ {threshold:.0%}, cíl kurz {odds_range}")
            if pool:
                break

        if used_threshold < min_prob:
            print(f"[TicketGenerator] Použit nižší práh {used_threshold:.0%} (místo {min_prob:.0%}) — nedostatek kandidátů při hlavním prahu")

        for c in pool[:5]:
            print(f"[TicketGenerator]   {c.home_team} vs {c.away_team}: {c.market_type.value} {c.selection} @ {c.odds} (p={round(c.probability,3)})")

        if pool_filter is not None:
            pool = pool_filter(pool)
            print(f"[TicketGenerator] po AI kontrole: {len(pool)} kandidátů")

        ticket = self._build_ticket(pool, odds_range, ticket_key, risk_level)

        # Fallback — pokud se tiket nepodařilo sestavit v cílovém rozsahu,
        # zkus postupně rozšiřovat rozsah kurzu a snižovat práh pravděpodobnosti
        if ticket is None and pool:
            wider_range = (odds_range[0] * 0.7, odds_range[1] * 1.5)
            print(f"[TicketGenerator] Zkouším širší rozsah kurzu: {wider_range}")
            ticket = self._build_ticket(pool, wider_range, ticket_key, risk_level)

        if ticket is None:
            # Poslední pokus — vezmi nejlepší dostupné výběry bez ohledu na rozsah
            all_thresholds = [0.60, 0.55]
            for threshold in all_thresholds:
                wider_pool = self._build_filtered_pool(matches, allowed_sports, allowed_markets, min_prob=threshold)
                if wider_pool:
                    any_range = (1.3, 20.0)
                    ticket = self._build_ticket(wider_pool, any_range, ticket_key, risk_level)
                    if ticket:
                        print(f"[TicketGenerator] Fallback tiket sestaven při prahu {threshold:.0%}")
                        break

        return {"safe": ticket, "aggressive": None}

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
        """AI načte nové zápasy — vyloučí dříve použité a sestaví znovu."""
        self._excluded_match_ids.update(previous_match_ids)
        filtered_matches = [m for m in matches if m.match_id not in self._excluded_match_ids]
        if not filtered_matches:
            self._excluded_match_ids.clear()  # pool vyčerpán, reset
            filtered_matches = matches
        return self.generate(
            filtered_matches, risk_level, allowed_sports, allowed_markets, time_frame_days, pool_filter
        )

    # ------------------------------------------------------------------
    def _build_filtered_pool(
        self, matches: list[MatchInput], allowed_sports: list[Sport], allowed_markets: list[MarketType],
        min_prob: float = MIN_SELECTION_PROBABILITY,
    ) -> list[SelectionCandidate]:
        pool: list[SelectionCandidate] = []
        for match in matches:
            if match.sport not in allowed_sports:
                continue
            candidates = MarketEvaluator.build_candidates(match, min_prob=min_prob)
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
        # risk_level > 50 => i v rámci >70% poolu povolí kombinovat slabší (ale stále platné) konce
        ordered_pool = pool if risk_level <= 50 else list(reversed(pool))

        # Preferuj mix typů trhů — střídej výhry a over góly místo samých over gólů.
        # Appka seřadí pool tak, aby výhry favoritů přišly dřív než další over góly
        # ze stejného zápasu, ale jinak zachová pořadí podle pravděpodobnosti.
        match_winner_first = [c for c in ordered_pool if c.market_type == MarketType.MATCH_WINNER]
        over_goals = [c for c in ordered_pool if c.market_type != MarketType.MATCH_WINNER]
        # Prokládáme: výhra, over, výhra, over...
        mixed = []
        mw_idx, og_idx = 0, 0
        while mw_idx < len(match_winner_first) or og_idx < len(over_goals):
            if mw_idx < len(match_winner_first):
                mixed.append(match_winner_first[mw_idx]); mw_idx += 1
            if og_idx < len(over_goals):
                mixed.append(over_goals[og_idx]); og_idx += 1
        ordered_pool = mixed

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
            print(f"[TicketGenerator] {ticket_type}: nepodařilo se sestavit (vybráno {len(selected)} kandidátů, dosažený kurz {round(running_odds,2)}, cíl {min_odds}-{max_odds})")
            return None  # nepodařilo se sestavit tiket v cílovém rozsahu kurzu z dostupných dat

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
        """
        Naivní násobení pravděpodobností jednotlivých výběrů předpokládá,
        že jsou na sobě úplně nezávislé. Dva zápasy ze STEJNÉ ligy ve
        STEJNÝ den ale částečně sdílí společné vlivy (rozhodcovské
        nařízení pro to kolo, počasí v regionu, formu soupeřů ovlivněnou
        stejným rozlosováním...) — žádný přesný kovarianční model na to
        appka nemá, ale aspoň hrubá penalizace je lepší než nulová.
        Za každou DALŠÍ dvojici výběrů ze stejné ligy+dne (nad první)
        appka kombinovanou pravděpodobnost mírně sníží.
        """
        league_day_counts: dict[tuple[str, str], int] = {}
        for c in selected:
            if not c.league or not c.kickoff_date:
                continue
            key = (c.league, c.kickoff_date)
            league_day_counts[key] = league_day_counts.get(key, 0) + 1

        extra_correlated_pairs = sum(max(0, count - 1) for count in league_day_counts.values())
        discount = CORRELATION_DISCOUNT_PER_EXTRA_SAME_LEAGUE_PAIR ** extra_correlated_pairs
        return combined_probability * discount
