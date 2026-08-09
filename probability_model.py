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
    "kratky":  (2.0, 3.0),     # Krátký — kurz 2–3 (jak je v UI)
    "stredni": (3.0, 6.0),     # Střední — kurz 3–6 (jak je v UI)
    "boost":   (10.0, 15.0),   # BOOST — kurz 10–15 (jak je v UI)
}

MAX_GOALS_FOR_SUM = 10  # horní mez pro sčítání Poissonova rozdělení (dostatečná přesnost)

KELLY_FRACTION = 0.25         # appka sází jen čtvrtinu plného Kelly výpočtu jako
                               # rezervu proti tomu, že náš odhad pravděpodobnosti
                               # není perfektní — plný Kelly je při nadhodnoceném
                               # modelu nebezpečně agresivní
MAX_RECOMMENDED_STAKE_PCT = 5.0  # tvrdý strop, i kdyby Kelly počítal víc

CORRELATION_DISCOUNT_PER_EXTRA_SAME_LEAGUE_PAIR = 0.95  # viz _apply_correlation_discount

MAX_MODEL_MARKET_GAP = 0.08  # appka model_probability pro edge/vklad neumožní vzdálit
                              # se od tržní pravděpodobnosti o víc než 8 pb (rozhodnuto
                              # 2026-08-01 na reálných datech: under gólů s průměrným
                              # rozdílem 11,3 pb mělo jen 44 % úspěšnost, over gólů
                              # s rozdílem 5,0 pb mělo 73 % — čím dál model utíká od
                              # trhu, tím spíš je model špatně, ne že appka "našla
                              # hodnotu". Viz edge_capped_model_probability.

MODEL_HIGH_CONFIDENCE_CAP = 0.75  # tvrdý strop na SUROVÝ model_probability (appka ho
                              # aplikuje ještě PŘED porovnáním s trhem, viz _candidate),
                              # rozhodnuto 2026-08-05 na appčiných vlastních vyhodnocených
                              # datech (appka porovnala 'appka odhaduje' vs. 'reálně
                              # vyhrálo' po koších): v koších 70–75 % appka byla dobře
                              # kalibrovaná (70→67,5 %, 75→83,3 %, dohromady přes 200
                              # vzorků), ale nad 75 % appka soustavně přestřelovala —
                              # 80→63,4 %, 85→70,6 %, 90→72,2 %, 95→52,6 % (appka
                              # nikdy spolehlivě netrefila to, co tvrdila). Model nad
                              # 75 % appce evidentně neumí rozlišit "jistější" od
                              # "míň jistého" — appka mu proto nedovolí tvrdit víc, než
                              # kolik appka reálně umí doložit. Aplikuje se JEN na
                              # model_probability (heuristický odhad), NE na tržní
                              # pravděpodobnost — tu appka nechává beze změny, reálný
                              # trh appka nepřepisuje.

FINAL_PROBABILITY_CAP = 0.85  # tvrdý strop na FINÁLNÍ (zobrazenou/stakingovou)
                              # pravděpodobnost, bez ohledu na to, jestli appka vzala
                              # model nebo tržní číslo — appka to přidala 2026-08-06,
                              # protože MODEL_HIGH_CONFIDENCE_CAP výš (0.75) chrání jen
                              # SUROVÝ model, ale appka na /admin/calibration-report
                              # živě potvrdila STEJNÝ přehnaně sebejistý vzorec i u
                              # KOŠŮ 80-100 % (kde appka většinou ukazuje TRŽNÍ
                              # pravděpodobnost, ne modelovou): 80→66,2 %, 85→71,6 %,
                              # 90→72,7 %, 95→63,2 %, 100→75 % reálně vyhrálo — i
                              # skutečný tržní kurz appce u nejtěsnějších favoritů (často
                              # exotické/nižší ligy s méně likvidním trhem) soustavně
                              # nadhodnocoval šanci. 0.85 appka volí o něco výš než
                              # MODEL_HIGH_CONFIDENCE_CAP — tržnímu číslu appka pořád
                              # věří víc než vlastnímu modelu, jen ne bezmezně.

# ---------------------------------------------------------------------
# Appčina VLASTNÍ kalibrační křivka — místo jednoho plochého stropu appka
# appce dovolí posunout finální pravděpodobnost směrem k tomu, co appka
# historicky OPRAVDU vídá v daném pravděpodobnostním koši (viz
# /admin/calibration-report a /admin/recompute-calibration-curve v
# backend_api.py). Appka appce tohle naplní na začátku KAŽDÉHO generování
# (set_calibration_curve) — modul appka drží bezstavový/čistý jinak, tohle
# je jediná výjimka, protože křivka appku potřebuje mimo probability_model.py
# (SQL nad appčinou databází), a threadovat ji jako parametr přes celý
# volací řetězec (MarketEvaluator → _candidate) appka vyhodnotila jako
# zbytečně invazivní pro jedno číslo.
_CALIBRATION_CURVE: dict[int, float] = {}


def set_calibration_curve(curve: dict[int, float]) -> None:
    global _CALIBRATION_CURVE
    _CALIBRATION_CURVE = curve or {}


def _apply_calibration_correction(probability: float) -> float:
    """Appka najde nejbližší koš po 5 % a POUZE STÁHNE appčino číslo dolů na to,
    co appka tam historicky OPRAVDU vyhrává — nikdy nahoru. Korekce je záměrně
    jednosměrná: křivka se počítá z malého vzorku (řádově dny/týdny), takže koš,
    kde appka historicky vyhrává VÍC než tvrdí, je s velkou pravděpodobností jen
    šum malého vzorku, ne skutečná nedoceněnost — kdyby appka takový koš tlačila
    nahoru, znovu by si zavedla přesně to přeceňování, kvůli kterému tahle
    korekce vznikla, jen jinou cestou. Beze změny, pokud appka křivku nemá
    načtenou nebo pro ten koš appka nemá uloženou hodnotu (viz
    CALIBRATION_BUCKET_MIN_SAMPLES v backend_api.py — příliš málo pozorování
    appka do křivky vůbec neuloží)."""
    if not _CALIBRATION_CURVE:
        return probability
    bucket = max(0, min(100, int(round(probability * 20)) * 5))
    real_pct = _CALIBRATION_CURVE.get(bucket)
    if real_pct is None:
        return probability
    return min(probability, real_pct / 100)


HT_GOAL_SHARE = 0.45  # jaký podíl z CELKOVÉHO očekávaného počtu gólů appka
                              # čeká už v prvním poločase — 0.45 je konzervativní
                              # střed běžně citovaného rozmezí (fotbalová
                              # statistika dlouhodobě ukazuje o něco víc gólů
                              # ve 2. poločase, cca 44-46 % v 1.). Appka to
                              # aplikuje na STEJNÉ home/away xG jako u
                              # celozápasového modelu (viz normalize_to_match_input
                              # v data_provider.py), žádný nový vstupní zdroj.

MIN_GAMES_PLAYED_FOR_FORM_SENSITIVE_MARKETS = 6  # pod tímhle appka týmu nedůvěřuje
                              # dost na to, aby nabídla over góly/BTTS (viz build_candidates)
                              # — s tak málo odehranými zápasy jede xG hlavně z
                              # LEAGUE_AVERAGE_GOALS_PER_TEAM (univerzální odhad), ne ze
                              # skutečné formy, a přesně tahle situace appce dřív vyráběla
                              # přehnaně sebevědomé (a prohrávající) tipy u týmů na začátku
                              # sezóny / v evropských kvalifikacích (rozhodnuto 2026-08-01,
                              # reálná data: 9 z 11 proher BTTS a skoro všechny prohry
                              # over_1.5 padly přesně na tenhle typ zápasu). Výhra a under
                              # góly appka tímhle neomezuje — tam appka problém neviděla.
                              # Původně appka nastavila 3, ale bez jistoty, kolik zápasů
                              # konkrétní opakovaně prohrávající kandidát (Deportivo Maldonado
                              # vs. Juventud, uruguayská Apertura) reálně měl, appka radši
                              # utáhla na 6 — víc jistoty na úkor trochu menšího poolu.

OVER_GOALS_STRICT_THRESHOLDS = {2.0, 2.5}  # appka na těchhle dvou prazích
                              # (2026-08-09) živě naměřila přes
                              # /admin/goals-market-calibration jen 61.5 %,
                              # resp. 54.5 % skutečnou úspěšnost (nižší
                              # prahy 1.5/1.75 appce jedou spolehlivě 71-85 %)
                              # — u over_2.5 appka navíc u prohraných výběrů
                              # měla v průměru o 11.4 p. b. sebejistější model
                              # než trh, jasný signál systematické přecenění,
                              # ne smůla na jednom zápase. Uživatel: "Zprisnu
                              # to" — appka na těchhle dvou prazích vyžaduje
                              # OVER_GOALS_STRICT_MIN_PROB místo běžného prahu.
OVER_GOALS_STRICT_MIN_PROB = 0.75

MATCH_WINNER_MIN_PROB = 0.65  # appčin absolutní tvrdý floor (uživatel: "Ok
                              # dame tedy limit 65% maximalne. Vse niz uz
                              # ne.") — appka na match_winner (2026-08-09,
                              # 84.2% skutečná úspěšnost, model tam appce
                              # vyšel MÍŇ sebejistý než trh) pouští favority
                              # dolů až na tenhle floor, i když ostatní trhy
                              # mají vyšší (71%) běžný požadavek.

BTTS_STRICT_MIN_PROB = 0.75  # appka (2026-08-09) přes /admin/all-markets-calibration
                              # živě naměřila jen 55.9 % skutečnou úspěšnost na BTTS
                              # (34 vzorků), u proher model v průměru o 10.9 p. b.
                              # sebejistější než trh — stejný systematický vzorec jako
                              # u over_2.0/over_2.5, appka na to reaguje stejně (vyšší
                              # práh místo běžných 65-71 %).

OVER_GOALS_EXCLUDED_COUNTRIES = {"Scotland"}  # skotská Premiership appce
                              # (2026-08-09) na over_goals vyšla jen 1 z 10
                              # (10 %) — appka ji na uživatelův pokyn
                              # ("vyradit skotskou ligu") z Over gólů úplně
                              # vyřazuje, ne jen zpřísňuje.

OVER_GOALS_MIN_TEAM_ATTACK_RATE = 1.4  # góly/zápas — appka pod tímhle
                              # tým nepovažuje za "útočný" (uživatel 2026-08-06:
                              # "Chci aby over tipy se vybírali opravdu z útočných
                              # týmů co dávají góly"). LEAGUE_AVERAGE_GOALS_PER_TEAM
                              # v data_provider.py je 1.3 (univerzální ligový průměr) —
                              # appka záměrně dala práh MÍRNĚ NAD průměr, ať "útočný"
                              # skutečně znamená nadprůměrně gólový tým, ne jen běžný.
                              # Kontrolováno na home_attack_rate/away_attack_rate
                              # (MatchInput) — appčina VLASTNÍ útočná forma týmu bez
                              # vlivu obrany soupeře, appka to NEPOUŽÍVÁ k výpočtu
                              # pravděpodobnosti (to pořád dělá home/away_expected_goals,
                              # co obranu soupeře zohledňuje), jen jako dodatečný filtr,
                              # ať appka nenabídne Over kvůli děravé obraně soupeře u
                              # týmu, co sám gólově nic nedokazuje.


def evaluate_selection_outcome(
    selection: "SelectionCandidate", home_goals: int, away_goals: int, total_cards: Optional[int] = None,
    ht_home_goals: Optional[int] = None, ht_away_goals: Optional[int] = None,
) -> Optional[bool]:
    """
    Vyhodnotí, jestli se tahle konkrétní selekce podle finálního výsledku
    potvrdila (True/False). Appka umí rozhodnout trhy odvozené ze skóre
    (MATCH_WINNER, OVER_GOALS, UNDER_GOALS, BTTS, DOUBLE_CHANCE) rovnou,
    OVER_CARDS tehdy, když jí appka dodá total_cards, a HT_OVER_GOALS/
    HT_UNDER_GOALS tehdy, když jí appka dodá poločasové skóre (appka ho
    dotahuje jen, když appka trh potřebuje — viz _settle_one_leg v
    backend_api.py, ať appka nevolá API-Football statistiky zbytečně u
    trhů, co je nepotřebují). Tenisové/basketbalové trhy appka pořád
    nevyhodnotí a vrátí None; tiket pak zůstane 'pending', dokud ho
    někdo nevyhodnotí jinak.
    """
    if selection.market_type == MarketType.MATCH_WINNER:
        if selection.selection == "home":
            return home_goals > away_goals
        if selection.selection == "away":
            return away_goals > home_goals
        if selection.selection == "draw":
            return home_goals == away_goals
        return None

    if selection.market_type == MarketType.DOUBLE_CHANCE:
        if selection.selection == "1X":
            return home_goals >= away_goals
        if selection.selection == "X2":
            return away_goals >= home_goals
        if selection.selection == "12":
            return home_goals != away_goals
        return None

    if selection.market_type == MarketType.OVER_GOALS:
        try:
            threshold = float(selection.selection.replace("over_", ""))
        except ValueError:
            return None
        return (home_goals + away_goals) > threshold

    if selection.market_type == MarketType.UNDER_GOALS:
        try:
            threshold = float(selection.selection.replace("under_", ""))
        except ValueError:
            return None
        return (home_goals + away_goals) < threshold

    if selection.market_type == MarketType.HT_OVER_GOALS:
        if ht_home_goals is None or ht_away_goals is None:
            return None
        try:
            threshold = float(selection.selection.replace("over_", ""))
        except ValueError:
            return None
        return (ht_home_goals + ht_away_goals) > threshold

    if selection.market_type == MarketType.HT_UNDER_GOALS:
        if ht_home_goals is None or ht_away_goals is None:
            return None
        try:
            threshold = float(selection.selection.replace("under_", ""))
        except ValueError:
            return None
        return (ht_home_goals + ht_away_goals) < threshold

    if selection.market_type == MarketType.BTTS:
        return home_goals >= 1 and away_goals >= 1

    if selection.market_type == MarketType.OVER_CARDS:
        if total_cards is None:
            return None
        try:
            threshold = float(selection.selection.replace("over_", ""))
        except ValueError:
            return None
        return total_cards > threshold

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
    UNDER_GOALS = "under_goals"              # fotbal/hokej — appka to drží jako VLASTNÍ typ trhu
                                              # (ne jen selekci v rámci OVER_GOALS), ať appka umí
                                              # under filtrovat/vyhodnotit nezávisle na over (viz
                                              # evaluate_selection_outcome, market_types filtr)
    BTTS = "btts"                            # fotbal — oba týmy dají gól (Both Teams To Score)
    OVER_CARDS = "over_cards"
    DOUBLE_CHANCE = "double_chance"          # fotbal — dvojtip (1X/X2/12), viz double_chance_probabilities
    HT_OVER_GOALS = "ht_over_goals"          # fotbal — góly v 1. poločase, stejný vzor jako OVER_GOALS/UNDER_GOALS
    HT_UNDER_GOALS = "ht_under_goals"
    OVER_GAMES = "over_games"               # tenis — celkový počet gamů v zápase
    OVER_ACES = "over_aces"                 # tenis — celkový počet es
    OVER_PENALTY_MINUTES = "over_penalty_minutes"  # hokej — trestné minuty
    OVER_POINTS = "over_points"              # basketbal — celkový počet bodů
    OVER_THREES = "over_threes"              # basketbal — celkový počet trojek


# Které trhy dávají u kterého sportu smysl — používá to i frontend (mapování
# nabízených chipů), aby se u tenisu nenabízel "Over gólů" apod.
SPORT_MARKETS: dict[Sport, list[MarketType]] = {
    Sport.FOOTBALL: [
        # UNDER_GOALS/HT_UNDER_GOALS appka přestala NABÍZET (uživatel
        # 2026-08-06: "Chci under odstranit dat jen over") — appka je
        # záměrně nechává v MarketType enumu i v MARKET_LABELS/
        # evaluate_selection_outcome (viz komentáře tam), ať appka historické
        # tikety s touhle nohou dál správně zobrazí a dosettluje. Jen appka
        # už žádného NOVÉHO kandidáta na under nevygeneruje (viz build_candidates).
        MarketType.MATCH_WINNER, MarketType.OVER_GOALS, MarketType.BTTS,
        MarketType.DOUBLE_CHANCE, MarketType.HT_OVER_GOALS,
    ],
    Sport.TENNIS: [MarketType.MATCH_WINNER, MarketType.OVER_GAMES, MarketType.OVER_ACES],
    Sport.HOCKEY: [MarketType.MATCH_WINNER, MarketType.OVER_GOALS, MarketType.UNDER_GOALS, MarketType.OVER_PENALTY_MINUTES],
    Sport.BASKETBALL: [MarketType.MATCH_WINNER, MarketType.OVER_POINTS, MarketType.OVER_THREES],
}

# Jedno místo pravdy pro lidsky čitelné popisky trhu/výběru — appka to
# sdílí mezi transparentním účtem (transparency_page.py), Telegram
# sázenkou (ticket_telegram.py) i frontendovým appkovým zobrazením tiketu,
# ať se popisky mezi appkou a appkou nikde nerozejdou.
MARKET_LABELS: dict[str, str] = {
    MarketType.MATCH_WINNER.value: "Vítěz zápasu",
    MarketType.OVER_GOALS.value: "Počet gólů",
    MarketType.UNDER_GOALS.value: "Počet gólů",
    MarketType.BTTS.value: "Oba týmy skórují",
    MarketType.OVER_CARDS.value: "Počet karet",
    MarketType.DOUBLE_CHANCE.value: "Dvojtip",
    MarketType.HT_OVER_GOALS.value: "Góly v poločase",
    MarketType.HT_UNDER_GOALS.value: "Góly v poločase",
    MarketType.OVER_GAMES.value: "Počet gamů",
    MarketType.OVER_ACES.value: "Počet es",
    MarketType.OVER_PENALTY_MINUTES.value: "Trestné minuty",
    MarketType.OVER_POINTS.value: "Počet bodů",
    MarketType.OVER_THREES.value: "Počet trojek",
}

SELECTION_LABELS: dict[str, str] = {
    "home": "Domácí", "away": "Hosté", "draw": "Remíza", "yes": "Ano", "no": "Ne",
    "1X": "1X (domácí nebo remíza)", "X2": "X2 (hosté nebo remíza)", "12": "12 (bez remízy)",
}


def market_label(code: Optional[str]) -> str:
    if not code:
        return ""
    return MARKET_LABELS.get(code, code.replace("_", " "))


def selection_label(code: Optional[str]) -> str:
    """'over_1.5' → 'Přes 1,5', 'under_2.5' → 'Pod 2,5', 'home' → 'Domácí'.
    Neznámý kód appka vrátí, jak přišel — radši srozumitelné torzo než prázdno."""
    if not code:
        return ""
    if code in SELECTION_LABELS:
        return SELECTION_LABELS[code]
    for prefix, word in (("over_", "Přes"), ("under_", "Pod")):
        if code.startswith(prefix):
            rest = code[len(prefix):]
            try:
                return f"{word} {float(rest):g}".replace(".", ",")
            except ValueError:
                return f"{word} {rest}".replace(".", ",")
    return code


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
# Dixon-Coles ÚTOČNÁ/OBRANNÁ SÍLA týmů (fitovaná přes celou ligu, ne jen
# odhadovaná z posledních zápasů dvou týmů jako _estimate_expected_goals
# v data_provider.py). Nízkoskórová tau korekce výše appka aplikuje VŽDY
# (rho=-0.13, literaturní hodnota) — tohle je ta druhá, chybějící
# polovina "opravdového" Dixon-Coles modelu: společný odhad síly VŠECH
# týmů v lize najednou, ne po dvou.
#
# Appka nemá k dispozici scipy/numpy (viz requirements.txt) — MLE fit
# nezávislého Poissonova modelu appka proto počítá iterativním
# proporcionálním fitováním (IPF, ekvivalentní Sinkhorn-Knopp škálování),
# což je standardní postup i v akademické literatuře pro tenhle typ
# modelu a dá se celý napsat čistým Pythonem bez optimalizační knihovny.
#
# DŮLEŽITÉ — appka tohle zapíná jen přes DIXON_COLES_ENABLED (viz
# backend_api.py), s tvrdým pádem zpátky na starý heuristický odhad
# (_estimate_expected_goals) pro KAŽDOU ligu/tým zvlášť, kde appka nemá
# dost odehraných zápasů nebo fit nekonverguje — appka radši použije
# starý, ověřený odhad, než aby nabídla tiket na nedostatečně podložený
# nový model.
# ---------------------------------------------------------------------
DIXON_COLES_HOME_ADVANTAGE = 1.10  # stejná hodnota jako v _estimate_expected_goals (data_provider.py)
DIXON_COLES_AWAY_FACTOR = 0.92
DIXON_COLES_MIN_MATCHES = 60       # appka nefituje ligu s málo odehranými zápasy — nespolehlivé
DIXON_COLES_MIN_TEAMS = 6          # appka nedůvěřuje fitu na hrstku týmů (malý pohár apod.)
DIXON_COLES_MAX_ITERATIONS = 200
DIXON_COLES_CONVERGENCE_TOL = 1e-4


def fit_dixon_coles_strengths(
    results: list[tuple[int, int, int, int]],
    home_advantage: float = DIXON_COLES_HOME_ADVANTAGE,
    away_factor: float = DIXON_COLES_AWAY_FACTOR,
) -> Optional[dict]:
    """
    results: seznam (home_team_id, away_team_id, home_goals, away_goals)
    za VŠECHNY odehrané zápasy jedné ligy/sezóny appka fituje najednou
    (viz data_provider.get_dixon_coles_strengths, které tenhle seznam
    appce sestaví z API-Football /fixtures?league&season&status=FT).

    Vrací None, pokud appka nemá dost dat na spolehlivý fit (viz
    DIXON_COLES_MIN_MATCHES/MIN_TEAMS) — appka radši nic, než aby
    appka fitovala na hrstce zápasů.

    Jinak vrací:
        {
            "teams": {team_id: {"attack": float, "defense": float}, ...},
            "league_avg_goals": float,  # průměr gólů na tým na zápas
            "sample_size": int,         # počet zápasů, ze kterých appka fitovala
            "converged": bool,
        }
    Attack/defense appka škáluje kolem 1.0 (ligový průměr) — attack=1.3
    znamená "dá o 30 % víc gólů, než průměrný tým ligy", defense=0.8
    znamená "inkasuje o 20 % míň, než průměrný tým ligy".
    """
    if len(results) < DIXON_COLES_MIN_MATCHES:
        return None

    team_ids: set[int] = set()
    for h, a, _, _ in results:
        team_ids.add(h)
        team_ids.add(a)
    if len(team_ids) < DIXON_COLES_MIN_TEAMS:
        return None

    league_avg_goals = sum(hg + ag for _, _, hg, ag in results) / (2 * len(results))
    if league_avg_goals <= 0:
        return None

    # Appka si pro každý tým předpočítá (soupeř, hrál_doma, góly_dal, góly_dostal)
    # napříč VŠEMI jeho zápasy — jeden průchod dat appce stačí, iterace pak
    # jen znovu a znovu přepočítávají attack/defense nad stejným seznamem.
    team_matches: dict[int, list[tuple[int, bool, int, int]]] = {t: [] for t in team_ids}
    for h, a, hg, ag in results:
        team_matches[h].append((a, True, hg, ag))
        team_matches[a].append((h, False, ag, hg))

    attack = {t: 1.0 for t in team_ids}
    defense = {t: 1.0 for t in team_ids}
    converged = False

    for _ in range(DIXON_COLES_MAX_ITERATIONS):
        new_attack: dict[int, float] = {}
        for t, matches in team_matches.items():
            expected_sum, actual_sum = 0.0, 0
            for opp, is_home, scored, _conceded in matches:
                factor = home_advantage if is_home else away_factor
                expected_sum += league_avg_goals * defense[opp] * factor
                actual_sum += scored
            new_attack[t] = actual_sum / expected_sum if expected_sum > 0 else attack[t]

        new_defense: dict[int, float] = {}
        for t, matches in team_matches.items():
            expected_sum, actual_sum = 0.0, 0
            for opp, is_home, _scored, conceded in matches:
                # Appka teď počítá s NOVĚ spočítaným attack soupeře (o řádek
                # výš) — rychlejší konvergence než čekat na další iteraci
                # (Gauss-Seidel varianta IPF, ne čistě "Jacobi").
                opp_factor = away_factor if is_home else home_advantage
                expected_sum += league_avg_goals * new_attack[opp] * opp_factor
                actual_sum += conceded
            new_defense[t] = actual_sum / expected_sum if expected_sum > 0 else defense[t]

        max_delta = max(
            max(abs(new_attack[t] - attack[t]) for t in team_ids),
            max(abs(new_defense[t] - defense[t]) for t in team_ids),
        )
        attack, defense = new_attack, new_defense
        if max_delta < DIXON_COLES_CONVERGENCE_TOL:
            converged = True
            break

    # Appka po fitu ještě přeškáluje na přesný ligový průměr 1.0 — IPF
    # k tomu konverguje přirozeně, ale malá numerická odchylka appce
    # zůstává, tohle ji smaže a dělá čísla čitelnější (attack=1.3 je pak
    # vždycky přesně "o 30 % nad průměrem ligy", ne "o 29.7 %").
    avg_attack = sum(attack.values()) / len(attack)
    avg_defense = sum(defense.values()) / len(defense)
    if avg_attack > 0:
        attack = {t: v / avg_attack for t, v in attack.items()}
    if avg_defense > 0:
        defense = {t: v / avg_defense for t, v in defense.items()}

    return {
        "teams": {t: {"attack": round(attack[t], 4), "defense": round(defense[t], 4)} for t in team_ids},
        "league_avg_goals": round(league_avg_goals, 4),
        "sample_size": len(results),
        "converged": converged,
    }


def dixon_coles_expected_goals(
    home_team_id: int, away_team_id: int, strengths: dict,
    home_advantage: float = DIXON_COLES_HOME_ADVANTAGE, away_factor: float = DIXON_COLES_AWAY_FACTOR,
) -> Optional[tuple[float, float]]:
    """
    Spočítá home_xg/away_xg ze zafitovaných sil (fit_dixon_coles_strengths).
    Vrací None, pokud appka pro NĚKTERÝ z týmů nemá zafitovaná čísla
    (nováček bez dost odehraných zápasů, chyba v párování ID...) — appka
    v takovém případě spadne zpátky na starý heuristický odhad (viz
    data_provider._estimate_expected_goals), ne že by appka cokoli
    dopočítávala napůl.
    """
    teams = strengths.get("teams", {})
    home = teams.get(home_team_id)
    away = teams.get(away_team_id)
    if home is None or away is None:
        return None
    league_avg = strengths["league_avg_goals"]
    home_xg = league_avg * home["attack"] * away["defense"] * home_advantage
    away_xg = league_avg * away["attack"] * home["defense"] * away_factor
    return round(home_xg, 3), round(away_xg, 3)


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
    kickoff_time: str = ""  # Čas výkopu HH:MM
    # Fotbal / hokej (góly modelované Poissonem)
    home_expected_goals: float = 0.0
    away_expected_goals: float = 0.0
    expected_cards: float = 0.0                # fotbal
    expected_penalty_minutes: float = 0.0      # hokej
    # Kolik zápasů má tým v AKTUÁLNÍ SEZÓNĚ odehráno — appka na start nové
    # sezóny (kdy tohle je u VŠECH týmů nutně nízké, viz
    # MIN_GAMES_PLAYED_FOR_FORM_SENSITIVE_MARKETS) nespoléhá SAMOTNÉ, právě
    # proto appka vedle toho nese i home/away_recent_form_available níž.
    home_games_played: int = 0
    away_games_played: int = 0
    # Appka má formu týmu i z posledních zápasů NAPŘÍČ sezónami (poslední
    # dokončené zápasy, ne jen ty v aktuální sezóně — viz get_recent_form/
    # adapt_recent_form_goals) — na začátku nové sezóny je tohle appčin
    # jediný skutečný zdroj formy, protože games_played výše je skoro u
    # všech týmů blízko nule. True = appka reálná (ne None) data dostala.
    home_recent_form_available: bool = False
    away_recent_form_available: bool = False
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

    # Očekávané góly jen za 1. poločas — appka je dopočítá z celozápasového
    # xG × HT_GOAL_SHARE (viz normalize_to_match_input), žádný nový vstupní
    # zdroj. Používá se jen pro ht_over_goals/ht_under_goals.
    home_expected_goals_ht: float = 0.0
    away_expected_goals_ht: float = 0.0

    # Čistě VLASTNÍ útočná forma týmu (shrinkage + nedávná forma), BEZ
    # vlivu domácí výhody/obrany soupeře/počasí/zranění — na rozdíl od
    # home_expected_goals/away_expected_goals appka tohle nepoužívá k
    # výpočtu pravděpodobnosti, jen jako filtr: appka "Over gólů" nabídne
    # pouze zápasy, kde OBA týmy samy o sobě skórují (viz
    # OVER_GOALS_MIN_TEAM_ATTACK_RATE) — appka nechce nabízet over jen
    # proto, že soupeř má děravou obranu, ale tým samotný gólově nic
    # nedokazuje (uživatel to výslovně chtěl, 2026-08-06: "Chci aby over
    # tipy se vybirali opravdu z utocnych tymu co davaji goly").
    home_attack_rate: float = 0.0
    away_attack_rate: float = 0.0

    favorite_win_market_odds: float = 1.0
    # True jen když favorite_win_market_odds pochází ze SKUTEČNÉHO tržního
    # kurzu (API-Football/the-odds-api/OddsPapi) — appka dřív bez reálného
    # kurzu cenu VYMÝŠLELA z vlastního modelu (viz normalize_to_match_input),
    # což appka živě potvrdila jako reálný problém důvěryhodnosti (2026-08-06,
    # rozdíly 2-50 % proti skutečným Tipsport cenám u zápasu Vlašim/PAOK/
    # Alianza). build_candidates teď MATCH_WINNER kandidáta nabídne jen
    # když je tenhle flag True — stejný princip appka už dřív měla u
    # UNDER_GOALS/HT_UNDER_GOALS (jen skutečný kurz, nic dopočítaného).
    favorite_odds_verified: bool = False
    over_goals_odds: dict[float, float] = field(default_factory=dict)            # {2.5: 1.85, ...}
    under_goals_odds: dict[float, float] = field(default_factory=dict)           # {2.5: 1.95, ...} — jen skutečné tržní kurzy, nikdy dopočítané z modelu
    btts_yes_odds: Optional[float] = None      # kurz na "oba týmy dají gól: ano"
    # Dvojtip a poločasové góly appka NEDOSTÁVÁ z hromadného odds-api
    # dotazu (ten appce vrací INVALID_MARKET) — appka je tahá zvlášť, jen
    # pro malou "shortlist" nejslibnějších zápasů, přes dotaz na
    # KONKRÉTNÍ zápas (viz _enrich_shortlist_with_extra_markets v
    # backend_api.py). Proto appka u těchhle dvou polí čeká, že budou
    # prázdná u VĚTŠINY zápasů — to je očekávané, ne chyba.
    double_chance_odds: dict[str, float] = field(default_factory=dict)           # {"1X": 1.25, "X2": 4.1, "12": 1.1}
    ht_over_goals_odds: dict[float, float] = field(default_factory=dict)         # {1.5: 1.93, ...}
    ht_under_goals_odds: dict[float, float] = field(default_factory=dict)        # {1.5: 1.85, ...} — jen skutečné tržní kurzy
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
    kickoff_time: str = ""
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
        """Krátké shrnutí celého tiketu — kolik výběrů, jaký kurz, jaký doporučený vklad."""
        return (
            f"{len(self.selections)} výběrů, celkový kurz {self.total_odds}, kombinovaná "
            f"pravděpodobnost {round(self.combined_probability * 100, 1)} %, doporučený vklad "
            f"{self.recommended_stake_pct} % bankrollu."
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

    @staticmethod
    def double_chance_probabilities(home_xg: float, away_xg: float) -> dict[str, float]:
        """1X/X2/12 appka odvodí ze STEJNÉ mřížky jako výhru/remízu/prohru —
        žádný nový model, jen jiný způsob součtu tří už spočítaných čísel."""
        winner = MarketEvaluator.match_winner_probabilities(home_xg, away_xg)
        return {
            "1X": winner["home"] + winner["draw"],
            "X2": winner["away"] + winner["draw"],
            "12": winner["home"] + winner["away"],
        }

    @staticmethod
    def ht_over_goals_probability(home_xg_ht: float, away_xg_ht: float, threshold: float) -> float:
        """Stejný Poisson/Dixon-Coles postup jako over_goals_probability,
        jen na poločasové (nižší) xG — viz HT_GOAL_SHARE."""
        grid = score_grid_probabilities(home_xg_ht, away_xg_ht)
        return sum(p for i, row in enumerate(grid) for j, p in enumerate(row) if i + j > threshold)

    @classmethod
    def build_candidates(cls, match: MatchInput, min_prob: float = MIN_SELECTION_PROBABILITY) -> list[SelectionCandidate]:
        """
        Vygeneruje kandidáty pro VŠECHNY relevantní trhy daného zápasu —
        které trhy to jsou, závisí na sportu (viz SPORT_MARKETS). Vrátí
        jen ty, jejichž model_probability >= min_prob (filtrace).
        """
        candidates: list[SelectionCandidate] = []

        if match.sport in (Sport.FOOTBALL, Sport.HOCKEY):
            # Góly modelované Poissonem — pro tyto dva sporty to dává smysl
            winner_probs = cls.match_winner_probabilities(
                match.home_expected_goals, match.away_expected_goals
            )
            favorite_side = max(winner_probs, key=winner_probs.get)
            if favorite_side != "draw" and match.favorite_odds_verified:
                candidates.append(cls._candidate(
                    match, MarketType.MATCH_WINNER, favorite_side,
                    winner_probs[favorite_side], match.favorite_win_market_odds,
                ))

            # Viz MIN_GAMES_PLAYED_FOR_FORM_SENSITIVE_MARKETS — over góly a
            # BTTS appka nenabídne, dokud o týmu nemá SPOLEHLIVÝ zdroj formy:
            # buď dost odehraných zápasů v AKTUÁLNÍ sezóně, NEBO (běžnější
            # případ) formu z posledních zápasů napříč sezónami (viz
            # home/away_recent_form_available). Bez týhle druhé podmínky by
            # appka na začátku každé nové sezóny přestala nabízet over
            # góly/BTTS prakticky VŠEM týmům ve VŠECH ligách najednou —
            # games_played v aktuální sezóně je tou dobou nutně nízké úplně
            # u každého, i u Bayernu (nahlásil uživatel 2026-08-01, ověřeno
            # v kódu — get_recent_form/adapt_recent_form_goals bere posledních
            # 10 DOKONČENÝCH zápasů bez ohledu na sezónu, takže tenhle zdroj
            # formy appce zůstává i v prvním kole). Výhra a under góly appka
            # tímhle vůbec neomezuje.
            home_reliable = (
                match.home_games_played >= MIN_GAMES_PLAYED_FOR_FORM_SENSITIVE_MARKETS
                or match.home_recent_form_available
            )
            away_reliable = (
                match.away_games_played >= MIN_GAMES_PLAYED_FOR_FORM_SENSITIVE_MARKETS
                or match.away_recent_form_available
            )
            has_reliable_form = home_reliable and away_reliable

            # Appka Under gólů kompletně zrušila (uživatel 2026-08-06:
            # "Chci under odstranit dat jen over") — appka teď nabízí
            # jen Over, a to navíc podmíněně: OBA týmy musí mít vlastní
            # útočnou formu aspoň OVER_GOALS_MIN_TEAM_ATTACK_RATE (viz
            # konstanta výše, MatchInput.home_attack_rate/away_attack_rate).
            # Appka dřív nabízela Over i u zápasů, kde appce vyšla vysoká
            # pravděpodobnost jen kvůli DĚRAVÉ OBRANĚ soupeře, ne proto,
            # že by tým sám o sobě skóroval — uživatel chtěl přesně tohle
            # odfiltrovat ("over tipy se vybírali opravdu z útočných týmů
            # co dávají góly").
            both_teams_attacking = (
                match.home_attack_rate >= OVER_GOALS_MIN_TEAM_ATTACK_RATE
                and match.away_attack_rate >= OVER_GOALS_MIN_TEAM_ATTACK_RATE
            )
            if has_reliable_form and both_teams_attacking and match.country not in OVER_GOALS_EXCLUDED_COUNTRIES:
                for threshold, odds in match.over_goals_odds.items():
                    prob = cls.over_goals_probability(match.home_expected_goals, match.away_expected_goals, threshold)
                    candidate = cls._candidate(match, MarketType.OVER_GOALS, f"over_{threshold}", prob, odds)
                    if threshold in OVER_GOALS_STRICT_THRESHOLDS and (
                        candidate.probability < OVER_GOALS_STRICT_MIN_PROB
                        or candidate.model_probability < OVER_GOALS_STRICT_MIN_PROB
                    ):
                        continue
                    candidates.append(candidate)

            # BTTS appka živě potvrdila jako nejslabší aktivní trh appky
            # (45 % win rate, appka to zjistila přes /admin/win-loss-report
            # 2026-08-06, výrazně pod Over gólů 73 % a Výhrou 94 %) — appka
            # mu teď dává STEJNOU podmínku útočnosti jako Over gólů výš
            # (both_teams_attacking), protože "oba dají gól" logicky
            # potřebuje přesně to samé — dva týmy, co samy skórují, ne
            # jen počet gólů nahnaný děravou obranou jedné strany.
            if match.sport == Sport.FOOTBALL and match.btts_yes_odds is not None and has_reliable_form and both_teams_attacking:
                prob = cls.btts_probability(match.home_expected_goals, match.away_expected_goals)
                candidate = cls._candidate(match, MarketType.BTTS, "yes", prob, match.btts_yes_odds)
                # appka (2026-08-09) přes /admin/all-markets-calibration živě
                # naměřila jen 55.9 % skutečnou úspěšnost na 34 vzorcích, u
                # proher navíc model v průměru o 10.9 p. b. sebejistější než
                # trh — stejný vzorec systematické přeceněnosti jako appka
                # opravila u over_2.0/over_2.5 (viz OVER_GOALS_STRICT_MIN_PROB).
                if candidate.probability >= BTTS_STRICT_MIN_PROB and candidate.model_probability >= BTTS_STRICT_MIN_PROB:
                    candidates.append(candidate)

            # Dvojtip a poločasové góly (2026-08-05) — appka je dostává jen
            # pro malou shortlist zápasů (viz _enrich_shortlist_with_extra_markets
            # v backend_api.py), takže double_chance_odds/ht_*_odds bude u
            # VĚTŠINY zápasů prázdné — appka to bere jako normální stav, ne
            # chybu, a kandidáta prostě nenabídne.
            if match.sport == Sport.FOOTBALL and match.double_chance_odds:
                dc_probs = cls.double_chance_probabilities(match.home_expected_goals, match.away_expected_goals)
                for selection, odds in match.double_chance_odds.items():
                    candidates.append(cls._candidate(match, MarketType.DOUBLE_CHANCE, selection, dc_probs[selection], odds))

            # Poločasové Under appka zrušila stejným pravidlem jako
            # celozápasové výš — appka nabízí jen poločasový Over, a i ten
            # jen s ověřenou útočnou formou obou týmů.
            if match.sport == Sport.FOOTBALL and has_reliable_form and both_teams_attacking:
                for threshold, odds in match.ht_over_goals_odds.items():
                    prob = cls.ht_over_goals_probability(match.home_expected_goals_ht, match.away_expected_goals_ht, threshold)
                    candidates.append(cls._candidate(match, MarketType.HT_OVER_GOALS, f"over_{threshold}", prob, odds))

            # Karty appka přestala nabízet (rozhodnuto 2026-08-01) — žádný
            # reálný tržní kurz appka na ně nikdy neměla (the-odds-api
            # nevrací "under" stranu, takže appka neuměla de-vigovat), jely
            # tak čistě na holém modelu bez pojistky. evaluate_selection_
            # outcome a MARKET_LABELS appka pro OVER_CARDS nechává, ať se
            # historické výběry v historii uživatelů dál vyhodnotí a
            # zobrazí správně (transparentnost > úklid).
            if match.sport == Sport.HOCKEY:
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
            if c.market_type == MarketType.DOUBLE_CHANCE:
                # Dvojtip appka nechává na nižší strop (1.05) než ostatní
                # trhy — smysl dvojtipu je právě nabídnout BEZPEČNOU nohu za
                # nízký kurz (appka ji kombinuje s jinou nohou, ať appka
                # trefí cílový rozsah kurzu tiketu), kdežto MIN_SELECTION_ODDS
                # (1.3) je nastavené kvůli over góly/kartám, kde nízký kurz
                # obvykle znamená appka nabízí "jistotu" bez skutečné hodnoty.
                return 1.05 <= c.odds <= MAX_SELECTION_ODDS
            return MIN_SELECTION_ODDS <= c.odds <= MAX_SELECTION_ODDS

        # DŮLEŽITÉ: appka dřív práh (70/65/60 %) kontrolovala jen na
        # model_probability — c.probability (co appka reálně ukáže, tržní
        # číslo když appka kurzy má) tak uměla vyjít o desítky bodů níž
        # (viz #109 — 93,8% model, 55,8% zobrazeno). Appka teď vyžaduje
        # OBĚ čísla nad prahem, ať práh platí i pro to, co appka doopravdy
        # posílá klientovi, ne jen pro appčin interní odhad.
        def effective_min_prob(c: SelectionCandidate) -> float:
            # appka (2026-08-09) přes /admin/all-markets-calibration živě
            # naměřila na match_winner 84.2% skutečnou úspěšnost (95 vzorků)
            # a model tam byl DOKONCE MÍŇ sebejistý než trh — na rozdíl od
            # over_2.5/BTTS appka tady nemá důkaz přeceněnosti, naopak. Appka
            # proto na výhře pouští i slabší favority (kurz cca 1.5) až na
            # appčin absolutní tvrdý floor (uživatel: "limit 65% maximalne,
            # vse niz uz ne") — na zbylých trzích běžný (přísnější) min_prob
            # zůstává beze změny.
            if c.market_type == MarketType.MATCH_WINNER:
                return min(min_prob, MATCH_WINNER_MIN_PROB)
            return min_prob

        return [
            c for c in candidates
            if c.model_probability >= effective_min_prob(c) and c.probability >= effective_min_prob(c) and passes_odds_filter(c)
        ]

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
        elif market_type == MarketType.UNDER_GOALS:
            threshold = selection.replace("under_", "")
            total_xg = round(match.home_expected_goals + match.away_expected_goals, 2)
            base = f"Součet očekávaných gólů obou týmů (xG celkem {total_xg}) dává {model_pct} % šanci na míň než {threshold} gólu/ů."
        elif market_type == MarketType.BTTS:
            base = (
                f"Při xG {match.home_expected_goals} (domácí) a {match.away_expected_goals} (hosté) "
                f"appka počítá {model_pct} % šanci, že skórují oba týmy."
            )
        elif market_type == MarketType.DOUBLE_CHANCE:
            dc_desc = {
                "1X": f"{match.home_team} nevyhraje", "X2": f"{match.away_team} nevyhraje", "12": "nebude remíza",
            }.get(selection, selection)
            base = f"Dvojtip {selection} ({dc_desc}) appka podle modelu odhaduje na {model_pct} %."
        elif market_type in (MarketType.HT_OVER_GOALS, MarketType.HT_UNDER_GOALS):
            prefix = "over_" if market_type == MarketType.HT_OVER_GOALS else "under_"
            threshold = selection.replace(prefix, "")
            word = "víc" if market_type == MarketType.HT_OVER_GOALS else "míň"
            total_xg_ht = round(match.home_expected_goals_ht + match.away_expected_goals_ht, 2)
            base = (
                f"Součet očekávaných gólů obou týmů jen za 1. poločas (xG {total_xg_ht}) "
                f"dává {model_pct} % šanci na {word} než {threshold} gólu/ů do poločasu."
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
    def _candidate(match: MatchInput, market_type: MarketType, selection: str, probability: float, odds: float,
                    market_key_type: Optional[MarketType] = None) -> SelectionCandidate:
        # model_probability je VŽDY náš vlastní heuristický odhad, nezávisle
        # na tom, jestli appka má tržní data. Pokud reálnou (de-vigovanou)
        # tržní pravděpodobnost pro tuhle přesnou selekci máme, použijeme ji
        # jako finální 'probability' pro staking — je to spolehlivější vstup
        # (viz devig_market výše) — ale model_probability si appka uchová
        # zvlášť, ať lze spočítat edge (viz SelectionCandidate.edge).
        #
        # market_key_type appka potřebuje jen u under gólů: SelectionCandidate
        # má market_type=UNDER_GOALS (aby šel nezávisle filtrovat/vyhodnotit,
        # viz evaluate_selection_outcome), ale _enrich_with_market_odds i
        # data_provider.adapt_*_odds appka ukládají tržní pravděpodobnost
        # under gólů pod klíč "over_goals:under_X" (jsou to dvě strany TÉŽE
        # tržní nabídky, appka je odjakživa páruje pod jednu). Bez týhle
        # výjimky by lookup pod "under_goals:under_X" nikdy nic nenašel a
        # appka by tak nedopatřením přeskočila kontrolu kladného edge u
        # KAŽDÉHO under výběru (viz require_positive_edge v _build_ticket).
        model_probability = min(probability, MODEL_HIGH_CONFIDENCE_CAP)
        market_key = f"{(market_key_type or market_type).value}:{selection}"
        market_probability = match.market_implied_probabilities.get(market_key)
        final_probability = market_probability if market_probability is not None else model_probability
        final_probability = _apply_calibration_correction(final_probability)
        final_probability = min(final_probability, FINAL_PROBABILITY_CAP)
        reasoning = MarketEvaluator._build_reasoning(match, market_type, selection, model_probability, market_probability)
        data_quality = MarketEvaluator._build_data_quality_note(match)
        return SelectionCandidate(
            match_id=match.match_id, home_team=match.home_team, away_team=match.away_team,
            sport=match.sport, market_type=market_type, selection=selection,
            probability=final_probability, odds=odds,
            model_probability=model_probability, market_probability=market_probability,
            league=match.league, country=match.country, league_id=match.league_id,
            kickoff_date=match.kickoff_date, kickoff_time=match.kickoff_time,
            reasoning=reasoning, data_quality=data_quality,
        )


def edge_capped_model_probability(selection: "SelectionCandidate") -> float:
    """
    model_probability pro účely edge/vkladu appka nenechá vzdálit se od
    tržní pravděpodobnosti o víc než MAX_MODEL_MARKET_GAP — bez tržního
    kurzu appka model neomezuje (nemá s čím srovnat).
    """
    if selection.market_probability is None:
        return selection.model_probability
    return min(selection.model_probability, selection.market_probability + MAX_MODEL_MARKET_GAP)


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
        risk_level: int,                     # 20=krátký, 50=střední, 80=boost
        allowed_sports: list[Sport],
        allowed_markets: list[MarketType],
        time_frame_days: int,
        pool_filter: Optional[Callable[[list[SelectionCandidate]], list[SelectionCandidate]]] = None,
    ) -> dict[str, Optional[Ticket]]:
        # Podle risk_level vyber jaký tiket postavit
        if risk_level <= 30:
            ticket_key = "kratky"
            min_prob = 0.71  # zvednuto z 0.70 (2026-08-05) — appka na kalibračních
            # datech zjistila, že koš 70 % vychází reálně na 67,5 %, kdežto 75 % na
            # 83,3 % (přes 200 vzorků), takže appka chtěla cílit výš — ale appka si
            # to ověřila přes /admin/candidate-pool-preview a 72-75 % appce na
            # aktuálním trhu zápasů nedávalo ANI JEDNOHO kandidáta (appka by vždycky
            # skončila na 65% dně, tedy žádná reálná změna). 71 % appka zvolila jako
            # nejpřísnější práh, co appce ještě reálně něco najde. 65% dno appka
            # NEMĚNÍ (uživatelovo explicitní rozhodnutí, viz FALLBACK_THRESHOLDS níž).
        elif risk_level <= 60:
            ticket_key = "stredni"
            min_prob = 0.71  # viz stejná poznámka u kratky výš
        else:
            ticket_key = "boost"
            min_prob = 0.55

        odds_range = TICKET_RANGES[ticket_key]

        # Fallback práh — zkusit postupně od nejpřísnějšího po nejvolnější.
        # DŮLEŽITÉ: Zkoušíme tiket, ne jen pool! (set+sorted, protože u
        # BOOSTu je min_prob=0.55 nižší než 0.60/0.65 — bez seřazení by se
        # smyčka zastavila hned na nejvolnějším prahu a nikdy by nezkusila
        # kvalitnější kandidáty napřed.)
        #
        # kratky/stredni appka na 60% dno záměrně přestala pouštět — appka
        # tam viděla zobrazené výběry i pod 55 % (de-vigovaná tržní cena,
        # viz probability), což u placeného kanálu vypadalo jako appka
        # neplní vlastní slib "aspoň 60/65 %". BOOST si svoje 55% dno (a
        # cestou k němu i 60 %) ponechává - je to jeho záměrně nejvolnější
        # tier, ne omyl.
        if ticket_key == "boost":
            FALLBACK_THRESHOLDS = sorted({min_prob, 0.65, 0.60}, reverse=True)
        else:
            FALLBACK_THRESHOLDS = sorted({min_prob, 0.65}, reverse=True)
        ticket = None
        used_threshold = min_prob
        candidate_counts = {}

        for threshold in FALLBACK_THRESHOLDS:
            pool = self._build_filtered_pool(matches, allowed_sports, allowed_markets, min_prob=threshold)
            used_threshold = threshold
            candidate_counts[int(threshold*100)] = len(pool)
            print(f"[{ticket_key}] {int(threshold*100)}%: {len(pool)} kandidátů")

            if not pool:
                continue  # Žádní kandidáti - zkusit nižší prah

            if pool_filter is not None:
                pool = pool_filter(pool)

            if ticket_key == "boost":
                # BOOST skládá 3+ výběrů na dlouhý kurz (10-15). I s reálnými
                # tržními kurzy se marže (vig) bookmakera s každou další
                # nohou násobí a appka navíc snižuje kombinovanou
                # pravděpodobnost, když víc výběrů sdílí ligu+den (časté u
                # kvalifikací) — kladný Kelly edge tak u dlouhé kombinace
                # prakticky nikdy nevyjde, i když jsou zápasy v pořádku. To
                # je matematická podstata parlaye, ne chyba dat. Kontrola na
                # kladný edge (viz _build_ticket) proto appka u BOOSTu vůbec
                # nevyžaduje — jinak by nešlo sestavit skoro žádný BOOST
                # tiket. U výhry favorita (MATCH_WINNER) appka někdy navíc
                # nemá žádnou NEZÁVISLOU tržní cenu (ani API-Football, ani
                # the-odds-api) a kurz si dopočítá sama z vlastní
                # pravděpodobnosti — appka proto nejdřív zkusí sestavit
                # tiket JEN z tržně ověřených výběrů (kvalitnější), a když
                # se to nepovede, použije jako záchrannou síť i neověřené.
                validated_pool = [
                    c for c in pool
                    if not (c.market_type == MarketType.MATCH_WINNER and c.market_probability is None)
                ]
                ticket = self._build_ticket(validated_pool, odds_range, ticket_key, risk_level, require_positive_edge=False)
                if ticket is None and len(validated_pool) < len(pool):
                    print(f"[{ticket_key}] Jen tržně ověřené výběry nestačily ({len(validated_pool)}/{len(pool)}), zkouším i neověřené")
                    ticket = self._build_ticket(pool, odds_range, ticket_key, risk_level, require_positive_edge=False)
            else:
                # Kladný edge (model_probability oproti reálnému kurzu,
                # viz _build_ticket) appka u kratky/stredni VYŽADUJE —
                # bez týhle kontroly appka nabízela tikety čistě podle
                # vlastní jistoty modelu, bez ohledu na to, jestli s tím
                # trh souhlasí (viz #48 — zrušeno po propadu stredni na
                # 0 % výher několik dní po sobě). Appka radši někdy tiket
                # nevygeneruje (FALLBACK_THRESHOLDS a MAX_EDGE_RETRIES v
                # _build_ticket to zkusí zmírnit), než aby nabídla sázku
                # bez prokázané výhody nad bookmakerem.
                ticket = self._build_ticket(pool, odds_range, ticket_key, risk_level, require_positive_edge=True)

            if ticket is not None:
                if threshold < min_prob:
                    print(f"[{ticket_key}] Tiket sestaven s prahem {int(threshold*100)}%")
                break  # Tiket se povedl! Skončit.

        if ticket is None:
            counts_str = ", ".join(f"{pct}%={n}" for pct, n in sorted(candidate_counts.items(), reverse=True))
            print(f"[{ticket_key}] Tiket se nepovedl. Kandidáti: {counts_str}")

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
            try:
                candidates = MarketEvaluator.build_candidates(match, min_prob=min_prob)
                pool.extend([c for c in candidates if c.market_type in allowed_markets])
            except Exception as e:
                print(f"[build_candidates ERROR] {match.home_team} vs {match.away_team}: {e}")
        pool.sort(key=lambda c: c.probability, reverse=True)
        return pool

    # Minimální počet výběrů a minimální kurz podle typu tiketu
    MIN_SELECTIONS = {
        "kratky": 2,    # Minimálně 2 výběry
        "stredni": 2,   # Minimálně 2 výběry
        "boost": 3,     # Minimálně 3 výběry
    }
    MIN_ODDS_HARD = {
        "kratky": 2.0,   # KRÁTKÝ: min 2.0 (dolní limit 2.0-3.0)
        "stredni": 3.0,  # STŘEDNÍ: min 3.0 (dolní limit 3.0-6.0)
        "boost": 10.0,   # BOOST: min 10.0 (dolní limit 10.0-15.0)
    }
    MAX_COMBO_LEGS = 10      # bezpečný strop na počet nohou (reálné kurzy nikdy nepotřebují víc)
    MAX_SEARCH_NODES = 50_000  # pojistka proti kombinatorickému výbuchu u velkých poolů
    MAX_EDGE_RETRIES = 5    # kolikrát appka zkusí kombinaci bez nejslabšího výběru (viz níže)

    def _build_ticket(
        self,
        pool: list[SelectionCandidate],
        odds_range: tuple[float, float],
        ticket_type: str,
        risk_level: int,
        require_positive_edge: bool = True,
    ) -> Optional[Ticket]:
        min_odds, max_odds = odds_range

        # Appka tu nejdřív (2026-08-05) vyřadí ověřené kandidáty se
        # SAMOTNÝMI záporným edge, ještě PŘED hledáním kombinace podle
        # kurzu — _search_combo totiž hledá čistě podle kurzu (seřazeno
        # podle pravděpodobnosti), takže si běžně vybere velké favority
        # na výhru (vysoká pravděpodobnost, ale kvůli MODEL_HIGH_CONFIDENCE_CAP
        # záporný edge) DŘÍV, než kvalitní kandidáty s nižší pravděpodobností
        # ale kladným edge (góly kolem 65-75 %). Odebírání "nejhorší nohy" AŽ
        # PO nalezení kombinace (viz níže) na tohle nestačilo — appka na
        # reálných datech (2026-08-05, /admin/edge-diagnostic) ověřila, že
        # i po týhle opravě appka pořád nesestavila tiket ze stovek
        # kandidátů, protože _search_combo si znovu a znovu vybíral skoro
        # STEJNOU (špatnou) kombinaci. Předfiltr je jednoduchý a spolehlivý:
        # appka do vyhledávání kombinace vůbec nepustí kandidáta, co SÁM
        # o sobě kazí edge — neověřené kandidáty (bez tržního kurzu) appka
        # nechává být, jejich příspěvek k edge je z podstaty neutrální
        # (viz require_positive_edge=False výjimka níže).
        if require_positive_edge:
            pool = [
                c for c in pool
                if c.market_probability is None or edge_capped_model_probability(c) * c.odds > 1.0
            ]

        # Vždy řaď sestupně podle pravděpodobnosti — chceme nejjistější výběry
        ordered_pool = sorted(pool, key=lambda c: c.probability, reverse=True)

        min_selections = self.MIN_SELECTIONS.get(ticket_type, 2)
        min_odds_hard = self.MIN_ODDS_HARD.get(ticket_type, 2.0)

        # _search_combo hledá kombinaci jen podle KURZU (padne do odds_range).
        # To appce může vrátit kombinaci, kde jednotlivé výběry sice prošly
        # filtrem na model_probability, ale zobrazovaná (tržně-preferovaná)
        # probability je nižší — takže výsledná kombinovaná pravděpodobnost
        # ×kurz nedá kladnou hodnotu a Kelly by doporučil vsadit 0 %. Appka
        # takovou kombinaci nikdy nevrátí jako hotový tiket (bylo by to
        # matoucí — appka sama tvrdí "nemá to cenu" a přesto ho nabídne) —
        # zkusí to znovu bez nejslabšího výběru z týhle kombinace.
        #
        # VÝJIMKA: require_positive_edge=False appka použije pro BOOSTovu
        # záchrannou (tržně-neověřenou) várku kandidátů — tam appka kurz
        # dopočítává sama jako 1/model_probability, takže edge je z podstaty
        # ~0 a korelační sleva (časté zápasy stejnou ligu+den) ho posune do
        # mírně záporných čísel u KAŽDÉ možné kombinace. Kontrola na kladný
        # edge by tak zahazovala úplně všechny kombinace bez ohledu na počet
        # kandidátů — appka by nikdy nic nevygenerovala, i když měla desítky
        # validních zápasů. Bez tržní ceny appka edge stejně nemůže ověřit,
        # tak tiket vrátí rovnou (recommended_stake_pct pak zobrazí 0 %,
        # ale appka nabídku nezablokuje).
        working_pool = ordered_pool
        retries = self.MAX_EDGE_RETRIES if require_positive_edge else 1
        for _ in range(retries):
            selected = self._search_combo(working_pool, min_odds, max_odds, min_selections, min_odds_hard)
            if selected is None:
                return None

            running_odds = 1.0
            for c in selected:
                running_odds *= c.odds

            combined_probability = 1.0
            for c in selected:
                combined_probability *= c.probability
            combined_probability = self._apply_correlation_discount(selected, combined_probability)

            # DŮLEŽITÉ: edge/vklad appka počítá z VLASTNÍHO modelu
            # (model_probability), ne z zobrazované/tržní pravděpodobnosti
            # (c.probability = de-vigovaná tržní hodnota, pokud appka má
            # kurzy). De-vigování z podstaty SNÍŽÍ pravděpodobnost pod
            # 1/kurz (tím se z implikované pravděpodobnosti odstraňuje
            # marže bookmakera) — když se pak taková (nižší) pravděpodobnost
            # znásobí s PŮVODNÍM (marži obsahujícím) kurzem, edge vyjde
            # téměř vždy lehce záporný, ÚPLNĚ BEZ OHLEDU na to, jestli náš
            # model s trhem souhlasí nebo ne. Kladný edge tak byl v praxi
            # nedosažitelný pro naprostou většinu tržně oceněných výběrů —
            # přesně to, co appka viděla u krátkého/středního tiketu se
            # stovkou kandidátů a přesto "Tiket se nepovedl". Model_probability
            # je NAŠE vlastní víra (nezávislá na trhu) — teprve srovnání
            # NAŠÍ pravděpodobnosti s REÁLNÝM kurzem dává smysluplný edge.
            #
            # model_probability appka navíc stropuje proti tržní (viz
            # edge_capped_model_probability, MAX_MODEL_MARKET_GAP) — čím
            # dál model utíká od trhu, tím spíš je model špatně, ne že
            # appka "našla hodnotu" (ověřeno na reálných datech 2026-08-01:
            # under gólů s průměrným rozdílem 11,3 pb mělo jen 44 %
            # úspěšnost, over gólů s rozdílem 5,0 pb mělo 73 %).
            edge_probability = 1.0
            for c in selected:
                edge_probability *= edge_capped_model_probability(c)
            edge_probability = self._apply_correlation_discount(selected, edge_probability)

            recommended_stake_pct = round(
                min(kelly_stake_fraction(edge_probability, running_odds) * 100, MAX_RECOMMENDED_STAKE_PCT), 1
            )

            # Appka kontrolu na kladný edge dělá jen na výběrech s OVĚŘENÝM
            # tržním kurzem (c.market_probability not None) — u neověřených
            # appka odds nastavuje na 1/model_probability (viz
            # normalize_to_match_input), takže jejich vlastní příspěvek
            # k edge je z podstaty přesně nulový (ani kladný, ani záporný)
            # a nemá smysl appku kvůli nim blokovat — appka jim věří stejně
            # jako modelu, bez umělého navyšování jistoty. Když appka NEMÁ
            # v kombinaci ani jeden ověřený výběr, nemá s čím porovnávat —
            # kontrolu appka přeskočí úplně (běží čistě na vlastním modelu).
            verified = [c for c in selected if c.market_probability is not None]
            if require_positive_edge and verified:
                v_running_odds = 1.0
                for c in verified:
                    v_running_odds *= c.odds
                v_edge_probability = 1.0
                for c in verified:
                    v_edge_probability *= edge_capped_model_probability(c)
                v_edge_probability = self._apply_correlation_discount(verified, v_edge_probability)
                edge_ok = kelly_stake_fraction(v_edge_probability, v_running_odds) > 0
            else:
                edge_ok = True

            if edge_ok:
                return Ticket(
                    ticket_type=ticket_type,
                    selections=selected,
                    total_odds=round(running_odds, 2),
                    combined_probability=round(combined_probability, 4),
                    recommended_stake_pct=recommended_stake_pct,
                )

            # Appka tu dřív odebírala "nejslabšího" podle SUROVÉ pravděpodobnosti
            # (min(verified, key=probability)) — to ale odebíralo špatnou nohu.
            # Velcí favorité na výhru mají vysokou tržní pravděpodobnost (appka
            # jim ale kvůli MODEL_HIGH_CONFIDENCE_CAP nevěří tolik jako trh),
            # takže mají ZÁPORNÝ příspěvek k edge, ale VYSOKOU probability —
            # appka je tak nikdy neoznačila za "nejslabší" a nechala je v
            # kombinaci, zatímco odebírala kvalitní kandidáty (góly s kladným
            # edge, ale nižší pravděpodobností kolem 65-75 %). Appka na
            # reálných datech 2026-08-05 ověřila (/admin/edge-diagnostic) —
            # appka měla přes 19 kandidátů s kladným edge, ale tiket se
            # přesto nikdy nesestavil. Appka teď odebírá nohu s NEJHORŠÍM
            # PŘÍSPĚVKEM K EDGE (edge_capped_model_probability × kurz, čím
            # níž pod 1.0, tím hůř), ne nejnižší pravděpodobností — přesně tu,
            # co kombinovaný edge kazí nejvíc.
            worst_edge = min(verified, key=lambda c: edge_capped_model_probability(c) * c.odds)
            working_pool = [c for c in working_pool if c is not worst_edge]

        return None

    def _search_combo(
        self,
        ordered_pool: list[SelectionCandidate],
        min_odds: float,
        max_odds: float,
        min_selections: int,
        min_odds_hard: float,
    ) -> Optional[list[SelectionCandidate]]:
        """
        Najde KOMBINACI výběrů z ordered_pool (ne nutně souvislý úsek —
        libovolnou podmnožinu), jejíž součin kurzů padne do [min_odds,
        max_odds]. Prostý greedy průchod seřazeným poolem (jak appka dělala
        dřív) občas o jeden výběr "přestřelí" horní hranici, výběr zahodí
        a už se k němu nikdy nevrátí — i když jiná kombinace ze STEJNÝCH
        kandidátů by do rozsahu trefila. To appce zbytečně shazovalo tikety,
        i když měla dost kvalitních zápasů.

        DFS s prořezáváním (branch & bound) prochází možnosti v pořadí
        klesající pravděpodobnosti (ordered_pool je už seřazený, takže
        větev "zahrnout" appka zkouší dřív než "přeskočit") — první nalezené
        řešení je tak zpravidla i to nejjistější. Dvě prořezávací podmínky
        drží prohledávání rychlé i pro desítky kandidátů:
          1) jakmile running_odds > max_odds, žádné DALŠÍ přidání ho nikdy
             nesníží (kurzy jsou vždy > 1) — větev je mrtvá, appka se
             nevrací.
          2) pokud by ani vynásobení VŠECH zbývajících kurzů nestačilo na
             min_odds, větev appka rovnou zahodí (suffix_max = optimistický
             horní odhad, ignoruje kolize stejného match_id — bezpečné,
             protože jen zmenšuje, ne zvětšuje, prořezávání).
        """
        n = len(ordered_pool)
        suffix_max = [1.0] * (n + 1)
        for i in range(n - 1, -1, -1):
            suffix_max[i] = suffix_max[i + 1] * ordered_pool[i].odds

        nodes = 0
        chosen: list[SelectionCandidate] = []
        used_matches: set[int] = set()

        def dfs(idx: int, running_odds: float) -> Optional[list[SelectionCandidate]]:
            # "Přeskoč kandidáta" appka řeší smyčkou, ne rekurzí — hloubka
            # rekurze tak závisí jen na počtu VYBRANÝCH noh (<= MAX_COMBO_LEGS),
            # ne na velikosti poolu. S pooly v řádu stovek/tisíců kandidátů
            # (viz zvýšený MAX_FIXTURES_PER_REQUEST) by rekurze jedna úroveň
            # na kandidáta klidně mohla narazit na limit rekurze Pythonu.
            nonlocal nodes
            while True:
                nodes += 1
                if nodes > self.MAX_SEARCH_NODES:
                    return None
                if running_odds > max_odds:
                    return None  # tahle větev už nikdy neklesne zpátky do rozsahu
                if (
                    len(chosen) >= min_selections
                    and running_odds >= min_odds_hard
                    and min_odds <= running_odds <= max_odds
                ):
                    return list(chosen)
                if idx >= n or len(chosen) >= self.MAX_COMBO_LEGS:
                    return None
                if running_odds * suffix_max[idx] < min_odds:
                    return None  # ani se vším zbývajícím by appka na min_odds nedosáhla

                candidate = ordered_pool[idx]
                if candidate.match_id not in used_matches:
                    used_matches.add(candidate.match_id)
                    chosen.append(candidate)
                    result = dfs(idx + 1, running_odds * candidate.odds)
                    chosen.pop()
                    used_matches.discard(candidate.match_id)
                    if result is not None:
                        return result

                idx += 1  # kandidáta appka nezahrnula — jede dál ve smyčce, ne rekurzí

        return dfs(0, 1.0)

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
