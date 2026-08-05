"""
availability.py — Výpočet volných termínů pro danou masérku a den.

Postup:
1. Zjisti pracovní okno(a) daného dne — nejdřív výjimka na konkrétní datum
   (schedule_overrides), jinak týdenní šablona (working_hours).
2. Z okna vygeneruj kandidátní starty slotů (po SLOT_GRANULARITY_MINUTES),
   dost dlouhé na zvolenou službu.
3. Odeber sloty, co se překrývají s ruční blokací nebo existující
   rezervací (pending/confirmed).
4. Na zbytek aplikuj "scarcity" — schválně skryj náhodnou část volných
   slotů, aby kalendář nepůsobil prázdně. Bere se DETERMINISTICKY podle
   (masérka, datum) — stejný den vrací stejný výsledek při opakovaném
   načtení stránky, mění se jen den ze dne. Skryté sloty se jen netváří
   jako obsazené ("obsazeno", bez vymyšleného jména/klienta) — appka
   záměrně NEpřipisuje skrytým slotům žádnou fiktivní identitu klienta,
   to by byl klamavý dark pattern (falešný důkaz poptávky).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date as Date, datetime, time as Time, timedelta
from zoneinfo import ZoneInfo

from .config import settings

TZ = ZoneInfo(settings.timezone_name)


@dataclass
class Slot:
    start: datetime
    end: datetime


def _local(d: Date, t: Time) -> datetime:
    return datetime.combine(d, t, tzinfo=TZ)


def get_work_windows(cur, masseuse_id: int, day: Date) -> list[tuple[Time, Time]]:
    cur.execute(
        "SELECT day_off, start_time, end_time FROM schedule_overrides "
        "WHERE masseuse_id = %s AND date = %s",
        (masseuse_id, day),
    )
    override = cur.fetchone()
    if override is not None:
        if override["day_off"]:
            return []
        return [(override["start_time"], override["end_time"])]

    weekday = day.weekday()  # 0=pondělí .. 6=neděle, sedí s uloženým sloupcem
    cur.execute(
        "SELECT start_time, end_time FROM working_hours "
        "WHERE masseuse_id = %s AND weekday = %s ORDER BY start_time",
        (masseuse_id, weekday),
    )
    return [(row["start_time"], row["end_time"]) for row in cur.fetchall()]


def get_busy_ranges(cur, masseuse_id: int, day: Date) -> list[tuple[datetime, datetime]]:
    day_start = _local(day, Time(0, 0))
    day_end = day_start + timedelta(days=1)
    ranges: list[tuple[datetime, datetime]] = []

    cur.execute(
        "SELECT start_at, end_at FROM blocked_slots "
        "WHERE masseuse_id = %s AND start_at < %s AND end_at > %s",
        (masseuse_id, day_end, day_start),
    )
    ranges.extend((row["start_at"], row["end_at"]) for row in cur.fetchall())

    cur.execute(
        "SELECT slot_start, slot_end FROM bookings "
        "WHERE masseuse_id = %s AND status IN ('pending', 'confirmed') "
        "AND slot_start < %s AND slot_end > %s",
        (masseuse_id, day_end, day_start),
    )
    ranges.extend((row["slot_start"], row["slot_end"]) for row in cur.fetchall())
    return ranges


def overlaps(start: datetime, end: datetime, ranges: list[tuple[datetime, datetime]]) -> bool:
    return any(start < r_end and end > r_start for r_start, r_end in ranges)


def slot_within_windows(
    day: Date, slot_start: datetime, slot_end: datetime, windows: list[tuple[Time, Time]]
) -> bool:
    return any(
        _local(day, w_start) <= slot_start and slot_end <= _local(day, w_end)
        for w_start, w_end in windows
    )


def _candidate_starts(
    windows: list[tuple[Time, Time]], day: Date, duration_minutes: int, granularity_minutes: int
) -> list[datetime]:
    starts: list[datetime] = []
    step = timedelta(minutes=granularity_minutes)
    duration = timedelta(minutes=duration_minutes)
    for window_start, window_end in windows:
        cursor = _local(day, window_start)
        window_end_dt = _local(day, window_end)
        while cursor + duration <= window_end_dt:
            starts.append(cursor)
            cursor += step
    return starts


def _scarcity_hide(
    free_starts: list[datetime], masseuse_id: int, day: Date, min_ratio: float, max_ratio: float
) -> list[datetime]:
    if not free_starts:
        return free_starts
    rng = random.Random(f"scarcity:{masseuse_id}:{day.isoformat()}")
    ratio = min_ratio + rng.random() * max(0.0, max_ratio - min_ratio)
    hide_count = round(len(free_starts) * ratio)
    if hide_count <= 0:
        return free_starts
    indices = list(range(len(free_starts)))
    rng.shuffle(indices)
    hidden = set(indices[:hide_count])
    return [s for i, s in enumerate(free_starts) if i not in hidden]


def get_available_slots(
    cur,
    masseuse_id: int,
    day: Date,
    duration_minutes: int,
    scarcity_min_ratio: float,
    scarcity_max_ratio: float,
    apply_scarcity: bool = True,
) -> list[Slot]:
    windows = get_work_windows(cur, masseuse_id, day)
    if not windows:
        return []

    busy = get_busy_ranges(cur, masseuse_id, day)
    granularity = settings.slot_granularity_minutes
    candidates = _candidate_starts(windows, day, duration_minutes, granularity)
    duration = timedelta(minutes=duration_minutes)
    free_starts = [c for c in candidates if not overlaps(c, c + duration, busy)]

    if apply_scarcity:
        free_starts = _scarcity_hide(free_starts, masseuse_id, day, scarcity_min_ratio, scarcity_max_ratio)

    return [Slot(start=s, end=s + duration) for s in sorted(free_starts)]
