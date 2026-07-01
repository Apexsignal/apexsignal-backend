"""
ApexSignal — Live Signal Engine
Modul: momentum_filter.py

Vyhodnocuje tlak týmu v reálném čase na základě klouzavého okna posledních
N minutových snapshotů. Klíčová podmínka zadání: odlišit 'Skutečný tlak'
(rostoucí konkrétní hrozby — střely na branku, nebezpečné útoky, xG) od
'Falešného držení míče' (vysoké possession % bez reálných šancí).
"""

from dataclasses import dataclass
from collections import deque
from enum import Enum
from typing import Optional, Deque, Dict


class PressureType(Enum):
    REAL_PRESSURE = "real_pressure"
    FALSE_POSSESSION = "false_possession"
    NEUTRAL = "neutral"


@dataclass
class MatchSnapshot:
    """Jeden vstupní záznam ze živého feedu (např. každou minutu)."""
    minute: int
    team: str               # 'home' nebo 'away' (případně 'match_id:home')
    shots_on_target: int
    shots_total: int
    possession_pct: float
    dangerous_attacks: int
    corners: int
    big_chances: int
    xg_cumulative: float
    cards: int = 0


@dataclass
class MomentumResult:
    """Výstup, který se mapuje na tabulku live_signals."""
    team: str
    momentum_score: float          # 0–100
    pressure_type: PressureType
    is_signal_worthy: bool
    confidence: float              # 0.0–1.0
    reasoning: str
    recommended_stake_pct: float   # navrhovaný vklad jako % bankrollu


class MomentumFilter:
    """
    Analyzuje sekvenci MatchSnapshot pro daný tým a vyhodnocuje, zda je
    aktuální tlak dostatečně silný a "skutečný" pro odeslání live signálu.

    Princip: nepracuje s jedním okamžikem, ale s TRENDEM v rámci klouzavého
    okna (WINDOW_SIZE minut) — to je to, co odlišuje skutečnou eskalaci tlaku
    od jednorázového náhodného momentu.
    """

    WINDOW_SIZE = 8          # počet posledních minutových záznamů v okně
    SIGNAL_THRESHOLD = 65.0  # minimální momentum_score pro odeslání signálu
    MIN_SNAPSHOTS_FOR_EVAL = 3

    # váhy jednotlivých metrik v celkovém skóre (musí dát součet 1.0)
    WEIGHTS = {
        "shots_on_target": 0.30,
        "dangerous_attacks": 0.25,
        "xg": 0.25,
        "big_chances": 0.15,
        "corners": 0.05,
    }

    def __init__(self) -> None:
        # klíč = identifikátor týmu/zápasu, value = klouzavé okno snapshotů
        self._history: Dict[str, Deque[MatchSnapshot]] = {}

    def ingest(self, snapshot: MatchSnapshot) -> Optional[MomentumResult]:
        """Vstupní bod — zavolat pro každý nový datový bod z live feedu."""
        window = self._history.setdefault(snapshot.team, deque(maxlen=self.WINDOW_SIZE))
        window.append(snapshot)

        if len(window) < self.MIN_SNAPSHOTS_FOR_EVAL:
            return None  # nedostatek dat pro trendovou analýzu

        return self._evaluate(window)

    # ------------------------------------------------------------------
    # Interní logika
    # ------------------------------------------------------------------

    def _evaluate(self, window: Deque[MatchSnapshot]) -> MomentumResult:
        first, last = window[0], window[-1]
        minutes_elapsed = max(last.minute - first.minute, 1)
        norm = 10 / minutes_elapsed  # normalizace na "tempo za 10 minut"

        sot_delta = last.shots_on_target - first.shots_on_target
        da_delta = last.dangerous_attacks - first.dangerous_attacks
        xg_delta = last.xg_cumulative - first.xg_cumulative
        bc_delta = last.big_chances - first.big_chances
        corners_delta = last.corners - first.corners

        sot_rate = sot_delta * norm
        da_rate = da_delta * norm
        xg_rate = xg_delta * norm
        bc_rate = bc_delta * norm
        corners_rate = corners_delta * norm

        raw_score = (
            self.WEIGHTS["shots_on_target"] * min(sot_rate * 20, 100)
            + self.WEIGHTS["dangerous_attacks"] * min(da_rate * 8, 100)
            + self.WEIGHTS["xg"] * min(xg_rate * 150, 100)
            + self.WEIGHTS["big_chances"] * min(bc_rate * 30, 100)
            + self.WEIGHTS["corners"] * min(corners_rate * 15, 100)
        )
        momentum_score = round(max(min(raw_score, 100), 0), 1)

        pressure_type = self._classify_pressure(last, sot_rate, da_rate, xg_rate)
        is_signal_worthy = (
            momentum_score >= self.SIGNAL_THRESHOLD
            and pressure_type == PressureType.REAL_PRESSURE
        )

        confidence = self._calc_confidence(window, momentum_score)
        reasoning = self._build_reasoning(
            last, sot_delta, da_delta, xg_delta, pressure_type, momentum_score
        )
        stake_pct = (
            self._recommend_stake(momentum_score, confidence) if is_signal_worthy else 0.0
        )

        return MomentumResult(
            team=last.team,
            momentum_score=momentum_score,
            pressure_type=pressure_type,
            is_signal_worthy=is_signal_worthy,
            confidence=confidence,
            reasoning=reasoning,
            recommended_stake_pct=stake_pct,
        )

    def _classify_pressure(
        self, last: MatchSnapshot, sot_rate: float, da_rate: float, xg_rate: float
    ) -> PressureType:
        """
        Klíčová podmínka zadání: vysoké possession % bez růstu konkrétních
        hrozeb (sterilní držení) NESMÍ být vyhodnoceno jako skutečný tlak.
        """
        high_possession = last.possession_pct >= 58.0
        low_threat_growth = sot_rate < 0.3 and da_rate < 1.6 and xg_rate < 0.07

        if high_possession and low_threat_growth:
            return PressureType.FALSE_POSSESSION

        real_threat = sot_rate >= 0.4 or da_rate >= 2.0 or xg_rate >= 0.1
        if real_threat:
            return PressureType.REAL_PRESSURE

        return PressureType.NEUTRAL

    def _calc_confidence(self, window: Deque[MatchSnapshot], momentum_score: float) -> float:
        """Vyšší jistota, pokud je nárůst střel na branku monotónní (ne nahodilý skok)."""
        sot_values = [s.shots_on_target for s in window]
        is_monotonic = all(a <= b for a, b in zip(sot_values, sot_values[1:]))
        base = momentum_score / 100
        bonus = 0.15 if is_monotonic else 0.0
        return round(min(base + bonus, 1.0), 2)

    def _recommend_stake(self, momentum_score: float, confidence: float) -> float:
        """Konzervativní stake sizing — i při maximálním skóre max. 5 % bankrollu."""
        base_stake = (momentum_score / 100) * confidence * 5.0
        return round(min(base_stake, 5.0), 1)

    def _build_reasoning(
        self,
        last: MatchSnapshot,
        sot_delta: int,
        da_delta: int,
        xg_delta: float,
        pressure_type: PressureType,
        score: float,
    ) -> str:
        if pressure_type == PressureType.FALSE_POSSESSION:
            return (
                f"Tým {last.team} drží míč ({last.possession_pct:.0f} %), ale bez nárůstu "
                f"konkrétních hrozeb (xG +{xg_delta:.2f}, střely na branku +{sot_delta}). "
                f"Vyhodnoceno jako sterilní držení — signál NEODESLÁN."
            )
        if pressure_type == PressureType.REAL_PRESSURE:
            return (
                f"Tým {last.team} vyvíjí skutečný tlak: +{sot_delta} střel na branku, "
                f"+{da_delta} nebezpečných útoků, nárůst xG {xg_delta:.2f}. "
                f"Momentum score {score}/100."
            )
        return f"Tým {last.team} bez výrazné změny tempa. Momentum score {score}/100."


# ============================================================
# Ukázkové použití (simulace 10 minut zápasu)
# ============================================================
if __name__ == "__main__":
    engine = MomentumFilter()

    # Scénář A: skutečný tlak — roste počet střel, nebezpečných útoků i xG
    real_pressure_feed = [
        MatchSnapshot(60, "home", shots_on_target=2, shots_total=5, possession_pct=48,
                       dangerous_attacks=8, corners=2, big_chances=0, xg_cumulative=0.9),
        MatchSnapshot(62, "home", shots_on_target=3, shots_total=7, possession_pct=50,
                       dangerous_attacks=10, corners=3, big_chances=1, xg_cumulative=1.1),
        MatchSnapshot(65, "home", shots_on_target=5, shots_total=10, possession_pct=53,
                       dangerous_attacks=14, corners=4, big_chances=2, xg_cumulative=1.5),
        MatchSnapshot(68, "home", shots_on_target=7, shots_total=13, possession_pct=55,
                       dangerous_attacks=18, corners=5, big_chances=3, xg_cumulative=1.9),
    ]

    # Scénář B: falešné držení míče — possession roste, ale bez reálných šancí
    false_possession_feed = [
        MatchSnapshot(60, "away", shots_on_target=1, shots_total=3, possession_pct=62,
                       dangerous_attacks=4, corners=1, big_chances=0, xg_cumulative=0.4),
        MatchSnapshot(63, "away", shots_on_target=1, shots_total=4, possession_pct=65,
                       dangerous_attacks=5, corners=1, big_chances=0, xg_cumulative=0.42),
        MatchSnapshot(67, "away", shots_on_target=1, shots_total=4, possession_pct=68,
                       dangerous_attacks=5, corners=2, big_chances=0, xg_cumulative=0.44),
    ]

    print("=== Scénář A: skutečný tlak ===")
    for snap in real_pressure_feed:
        result = engine.ingest(snap)
        if result:
            print(f"min {snap.minute}: score={result.momentum_score} "
                  f"type={result.pressure_type.value} signal={result.is_signal_worthy}")
            print(f"  -> {result.reasoning}")

    print("\n=== Scénář B: falešné držení míče ===")
    for snap in false_possession_feed:
        result = engine.ingest(snap)
        if result:
            print(f"min {snap.minute}: score={result.momentum_score} "
                  f"type={result.pressure_type.value} signal={result.is_signal_worthy}")
            print(f"  -> {result.reasoning}")
