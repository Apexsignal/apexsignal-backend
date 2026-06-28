"""
ApexSignal — Live Signal Engine
Modul: MomentumFilter

Účel:
    Zpracovává real-time data ze zápasu (minutu po minutě) a rozhoduje,
    zda aktuální tlak jednoho z týmů je "Skutečný tlak" (reálná šance na gól,
    podpořená střelami/šancemi), nebo jen "Falešné držení míče" (vysoká
    procenta držení míče bez reálného ohrožení branky).

    Pokud je tlak vyhodnocen jako skutečný a překročí prahové hodnoty,
    modul vygeneruje strukturovaný signál připravený k zápisu do tabulky
    `live_signals` a k odeslání jako notifikace uživateli.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
from enum import Enum
from typing import Optional, Deque


class SignalType(str, Enum):
    ENTRY = "entry"          # nový doporučený vstup do sázky
    CASHOUT = "cashout"      # doporučení odprodat/cash-out
    ADJUST = "adjust"        # doporučení upravit/snížit sázku


@dataclass
class MatchSnapshot:
    """Jeden datový bod z timeline zápasu (1 řádek z match_stats_timeline)."""
    minute: int
    home_possession: int
    away_possession: int
    home_shots_on_target: int
    away_shots_on_target: int
    home_dangerous_attacks: int
    away_dangerous_attacks: int
    home_corners: int = 0
    away_corners: int = 0
    red_cards_home: int = 0
    red_cards_away: int = 0
    home_goals: int = 0
    away_goals: int = 0


@dataclass
class MomentumSignal:
    """Výstupní struktura odpovídající poli notifikace v zadání."""
    match_id: int
    market: str                      # Doporučený sázkový trh
    reasoning: str                   # Zdůvodnění analýzy
    recommended_stake_pct: float     # Doporučený vklad (%)
    signal_type: SignalType
    is_real_pressure: bool
    momentum_score_team: float
    team_side: str                    # "home" / "away"
    odds: Optional[float] = None      # doplní backend_api.py z get_live_odds() před odesláním;
                                       # None = appka kurz nesehnala (např. beta endpoint
                                       # /odds/live ještě nemáš povolený na svém API-Football účtu)


class MomentumFilter:
    """
    Vyhodnocuje tlak v zápase na základě klouzavého okna posledních N minut.

    Klíčová logika:
        pressure_index  = váhovaný součet (střely na branku, nebezpečné útoky, rohy)
                           normalizovaný na minutu v okně.
        possession_share = podíl držení míče týmu v okně.

        "Skutečný tlak"     -> pressure_index je vysoký A JEHO podíl mezi týmy
                                koresponduje (nebo převyšuje) podíl držení míče.
        "Falešné držení míče" -> possession_share je vysoký, ale pressure_index
                                  zůstává nízký (míč se "přehazuje", bez šancí).
    """

    # váhy jednotlivých metrik v pressure_index
    WEIGHT_SHOTS_ON_TARGET = 3.0
    WEIGHT_DANGEROUS_ATTACK = 1.0
    WEIGHT_CORNER = 0.5

    # prahové hodnoty pro rozhodování
    MIN_PRESSURE_INDEX_FOR_SIGNAL = 4.0     # minimální síla tlaku v okně
    MIN_PRESSURE_SHARE_VS_POSSESSION = 0.9  # pressure_share musí být >= 90 % possession_share
    WINDOW_MINUTES = 10                      # délka klouzavého okna
    GOAL_COOLDOWN_MINUTES = 5    # po vstřeleném gólu appka chvíli "ztichne" — stav zápasu
                                  # se právě zásadně změnil, staré okno tlaku už není relevantní
    CONFIRMATION_TICKS = 2       # tlak musí kvalifikovat na signál ve dvou po sobě jdoucích
                                  # vyhodnoceních, ne jen jednou — odfiltruje krátké špičky
    GAME_STATE_TRAILING_BOOST = 1.15   # tým prohrává a aktivně dotahuje — vyšší urgentnost
    GAME_STATE_BIG_LEAD_DAMPEN = 0.85  # tým vede o 2+ gólů — tlak může být jen formální

    def __init__(self, match_id: int, window_minutes: int = WINDOW_MINUTES):
        self.match_id = match_id
        self.window_minutes = window_minutes
        self._window: Deque[MatchSnapshot] = deque()
        self._last_signal_minute: Optional[int] = None
        self._last_event_flags: dict[str, bool] = {
            "red_card_home": False,
            "red_card_away": False,
        }
        self._last_known_goals: dict[str, int] = {"home": 0, "away": 0}
        self._cooldown_until_minute: Optional[int] = None
        self._qualifying_streak: dict = {"team_side": None, "count": 0}

    # ------------------------------------------------------------------
    # Veřejné API
    # ------------------------------------------------------------------
    def ingest(self, snapshot: MatchSnapshot) -> Optional[MomentumSignal]:
        """
        Přijme nový datový bod ze zápasu, aktualizuje klouzavé okno
        a vrátí MomentumSignal, pokud jsou splněny podmínky pro odeslání
        signálu (entry) nebo smart-correction (cashout/adjust). Jinak None.
        """
        self._handle_goal_change(snapshot)
        self._push_to_window(snapshot)
        smart_correction = self._check_smart_correction(snapshot)
        if smart_correction is not None:
            return smart_correction

        if self._cooldown_until_minute is not None and snapshot.minute < self._cooldown_until_minute:
            return None  # appka je v klidovém režimu po gólu — žádné nové entry signály

        return self._evaluate_entry_signal()

    def _handle_goal_change(self, snapshot: MatchSnapshot) -> None:
        """
        Pokud se skóre od poslední zprávy změnilo, appka pozná, že právě
        padl gól — vyčistí klouzavé okno (tlak nashromážděný PŘED gólem
        už neodpovídá nové herní situaci) a na pár minut "ztichne", než
        začne hodnotit zápas znovu od nuly.
        """
        if (snapshot.home_goals != self._last_known_goals["home"]
                or snapshot.away_goals != self._last_known_goals["away"]):
            self._last_known_goals = {"home": snapshot.home_goals, "away": snapshot.away_goals}
            self._window.clear()
            self._qualifying_streak = {"team_side": None, "count": 0}
            self._cooldown_until_minute = snapshot.minute + self.GOAL_COOLDOWN_MINUTES

    # ------------------------------------------------------------------
    # Interní logika
    # ------------------------------------------------------------------
    def _push_to_window(self, snapshot: MatchSnapshot) -> None:
        self._window.append(snapshot)
        cutoff = snapshot.minute - self.window_minutes
        while self._window and self._window[0].minute < cutoff:
            self._window.popleft()

    def _aggregate_window(self) -> dict:
        """Sečte metriky v okně pro oba týmy."""
        agg = {
            "home_sot": 0, "away_sot": 0,
            "home_da": 0, "away_da": 0,
            "home_corners": 0, "away_corners": 0,
            "home_poss_sum": 0, "away_poss_sum": 0,
        }
        for s in self._window:
            agg["home_sot"] += s.home_shots_on_target
            agg["away_sot"] += s.away_shots_on_target
            agg["home_da"] += s.home_dangerous_attacks
            agg["away_da"] += s.away_dangerous_attacks
            agg["home_corners"] += s.home_corners
            agg["away_corners"] += s.away_corners
            agg["home_poss_sum"] += s.home_possession
            agg["away_poss_sum"] += s.away_possession
        return agg

    def _pressure_index(self, sot: int, da: int, corners: int) -> float:
        n = max(len(self._window), 1)
        raw = (
            sot * self.WEIGHT_SHOTS_ON_TARGET
            + da * self.WEIGHT_DANGEROUS_ATTACK
            + corners * self.WEIGHT_CORNER
        )
        return round(raw / n, 2)

    def _evaluate_entry_signal(self) -> Optional[MomentumSignal]:
        if len(self._window) < 3:
            self._qualifying_streak = {"team_side": None, "count": 0}
            return None  # nedostatek dat pro spolehlivé vyhodnocení

        agg = self._aggregate_window()
        n = len(self._window)

        home_pressure = self._pressure_index(agg["home_sot"], agg["home_da"], agg["home_corners"])
        away_pressure = self._pressure_index(agg["away_sot"], agg["away_da"], agg["away_corners"])

        home_possession_share = agg["home_poss_sum"] / (n * 100)
        away_possession_share = agg["away_poss_sum"] / (n * 100)

        total_pressure = home_pressure + away_pressure or 1e-6
        home_pressure_share = home_pressure / total_pressure
        away_pressure_share = away_pressure / total_pressure

        # vyber tým s vyšším tlakem jako kandidáta na signál
        if home_pressure >= away_pressure:
            team_side, pressure, pressure_share, possession_share = (
                "home", home_pressure, home_pressure_share, home_possession_share,
            )
        else:
            team_side, pressure, pressure_share, possession_share = (
                "away", away_pressure, away_pressure_share, away_possession_share,
            )

        is_real_pressure = self._is_real_pressure(pressure, pressure_share, possession_share)
        qualifies = is_real_pressure and pressure >= self.MIN_PRESSURE_INDEX_FOR_SIGNAL

        if not qualifies:
            self._qualifying_streak = {"team_side": None, "count": 0}
            return None

        # Potvrzení přes víc po sobě jdoucích vyhodnocení — jedna krátká
        # špička tlaku signál nespustí, musí vydržet aspoň CONFIRMATION_TICKS
        # volání ingest() v řadě (a pro stejný tým).
        if self._qualifying_streak["team_side"] == team_side:
            self._qualifying_streak["count"] += 1
        else:
            self._qualifying_streak = {"team_side": team_side, "count": 1}

        if self._qualifying_streak["count"] < self.CONFIRMATION_TICKS:
            return None

        # zabraň spamování stejným signálem opakovaně po sobě
        current_minute = self._window[-1].minute
        if self._last_signal_minute is not None and current_minute - self._last_signal_minute < 5:
            return None
        self._last_signal_minute = current_minute

        confidence = min(pressure / (self.MIN_PRESSURE_INDEX_FOR_SIGNAL * 2), 1.0)
        state_modifier, state_note = self._game_state_modifier(team_side)
        confidence = min(confidence * state_modifier, 1.0)
        recommended_stake_pct = round(1.0 + confidence * 2.0, 1)  # rozsah cca 1-3 %

        reasoning = (
            f"Tým {team_side} vykazuje pressure_index {pressure} v posledních "
            f"{self.window_minutes} min, podíl tlaku {round(pressure_share * 100, 1)} % "
            f"vs. držení míče {round(possession_share * 100, 1)} % — tlak je podpořen "
            f"reálnými šancemi (střely na branku, nebezpečné útoky), nejde o pouhé "
            f"přehazování míče."
        )
        if state_note:
            reasoning += f" ({state_note})"

        return MomentumSignal(
            match_id=self.match_id,
            market="Next Goal",
            reasoning=reasoning,
            recommended_stake_pct=recommended_stake_pct,
            signal_type=SignalType.ENTRY,
            is_real_pressure=True,
            momentum_score_team=pressure,
            team_side=team_side,
        )

    def _game_state_modifier(self, team_side: str) -> tuple[float, Optional[str]]:
        """
        Tým, co prohrává a aktivně dotahuje skóre, je v jiné situaci než
        tým, co už vede o pár gólů a tlak může být jen formální (hraje si
        s výsledkem, ne o vyrovnání). Vrací (multiplikátor důvěry, poznámka).
        """
        if not self._window:
            return 1.0, None
        latest = self._window[-1]
        team_goals = latest.home_goals if team_side == "home" else latest.away_goals
        opp_goals = latest.away_goals if team_side == "home" else latest.home_goals
        diff = team_goals - opp_goals
        if diff < 0:
            return self.GAME_STATE_TRAILING_BOOST, "tým prohrává a aktivně dotahuje skóre"
        if diff >= 2:
            return self.GAME_STATE_BIG_LEAD_DAMPEN, "tým vede o 2+ gólů, tlak může být jen formální"
        return 1.0, None

    def _is_real_pressure(self, pressure: float, pressure_share: float, possession_share: float) -> bool:
        """
        Klíčové rozlišení zadání:
        Pokud possession_share je vysoký, ale pressure_share neodpovídá
        (tým má míč, ale nevytváří šance), jde o "Falešné držení míče".
        """
        if possession_share <= 0:
            return pressure_share >= 0.5
        coherence_ratio = pressure_share / possession_share
        return coherence_ratio >= self.MIN_PRESSURE_SHARE_VS_POSSESSION

    def _check_smart_correction(self, snapshot: MatchSnapshot) -> Optional[MomentumSignal]:
        """
        Detekuje náhlé změny podmínek (červená karta) a vrací doporučení
        na cash-out / úpravu sázky.
        """
        if snapshot.red_cards_home > 0 and not self._last_event_flags["red_card_home"]:
            self._last_event_flags["red_card_home"] = True
            return MomentumSignal(
                match_id=self.match_id,
                market="Cash-out doporučení",
                reasoning=(
                    f"Domácí tým obdržel červenou kartu v {snapshot.minute}. minutě. "
                    f"Herní podmínky se výrazně změnily, doporučujeme přehodnotit "
                    f"otevřené pozice na tento zápas."
                ),
                recommended_stake_pct=0.0,
                signal_type=SignalType.CASHOUT,
                is_real_pressure=False,
                momentum_score_team=0.0,
                team_side="home",
            )

        if snapshot.red_cards_away > 0 and not self._last_event_flags["red_card_away"]:
            self._last_event_flags["red_card_away"] = True
            return MomentumSignal(
                match_id=self.match_id,
                market="Cash-out doporučení",
                reasoning=(
                    f"Hostující tým obdržel červenou kartu v {snapshot.minute}. minutě. "
                    f"Herní podmínky se výrazně změnily, doporučujeme přehodnotit "
                    f"otevřené pozice na tento zápas."
                ),
                recommended_stake_pct=0.0,
                signal_type=SignalType.CASHOUT,
                is_real_pressure=False,
                momentum_score_team=0.0,
                team_side="away",
            )

        return None


# -------------------------------------------------------------------------
# Ukázkové použití (lze odstranit / přesunout do testů)
# -------------------------------------------------------------------------
if __name__ == "__main__":
    mf = MomentumFilter(match_id=101)

    sample_data = [
        MatchSnapshot(minute=60, home_possession=65, away_possession=35,
                      home_shots_on_target=0, away_shots_on_target=1,
                      home_dangerous_attacks=2, away_dangerous_attacks=1),
        MatchSnapshot(minute=62, home_possession=68, away_possession=32,
                      home_shots_on_target=1, away_shots_on_target=0,
                      home_dangerous_attacks=3, away_dangerous_attacks=0),
        MatchSnapshot(minute=64, home_possession=70, away_possession=30,
                      home_shots_on_target=2, away_shots_on_target=0,
                      home_dangerous_attacks=4, away_dangerous_attacks=1,
                      home_corners=2),
        MatchSnapshot(minute=66, home_possession=72, away_possession=28,
                      home_shots_on_target=3, away_shots_on_target=0,
                      home_dangerous_attacks=5, away_dangerous_attacks=0,
                      home_corners=3),
    ]

    for point in sample_data:
        result = mf.ingest(point)
        if result:
            print(f"[{point.minute}'] SIGNAL -> {result.team_side} | "
                  f"stake {result.recommended_stake_pct}% | {result.reasoning}")
