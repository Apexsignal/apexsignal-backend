"""schemas.py — Pydantic modely pro request/response těla."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

PHONE_RE = re.compile(r"^(\+420)?\s*(\d{3})\s*(\d{3})\s*(\d{3})$")


def normalize_czech_phone(raw: str) -> str:
    match = PHONE_RE.match(raw.strip())
    if not match:
        raise ValueError("Telefonní číslo musí být české (9 číslic, volitelně s +420)")
    _, a, b, c = match.groups()
    return f"+420{a}{b}{c}"


class ServiceOut(BaseModel):
    id: int
    name: str
    duration_minutes: int
    price_kc: Optional[int] = None


class MasseuseOut(BaseModel):
    id: int
    name: str


class SlotOut(BaseModel):
    start: datetime
    end: datetime


class AvailabilityResponse(BaseModel):
    masseuse_id: int
    date: date
    service_id: int
    slots: list[SlotOut]


class BookingCreateRequest(BaseModel):
    masseuse_id: int
    service_id: int
    slot_start: datetime
    client_name: Optional[str] = Field(default=None, max_length=100)
    client_phone: str

    @field_validator("client_phone")
    @classmethod
    def _validate_phone(cls, value: str) -> str:
        return normalize_czech_phone(value)


class BookingCreateResponse(BaseModel):
    booking_id: int
    status: str
    message: str


class WorkingHourIn(BaseModel):
    weekday: int = Field(ge=0, le=6)
    start_time: str  # "HH:MM"
    end_time: str


class ScheduleOverrideIn(BaseModel):
    date: date
    day_off: bool = False
    start_time: Optional[str] = None
    end_time: Optional[str] = None


class BlockedSlotIn(BaseModel):
    start_at: datetime
    end_at: datetime
    reason: Optional[str] = Field(default=None, max_length=255)


class MasseuseCreate(BaseModel):
    name: str = Field(max_length=100)
    phone: Optional[str] = None
    # None = appka doplní hodnotu z prostředí (SCARCITY_MIN_RATIO/SCARCITY_MAX_RATIO)
    # v admin_create_masseuse, ne tady natvrdo — tenhle soubor settings neimportuje.
    scarcity_min_ratio: Optional[float] = Field(default=None, ge=0, le=1)
    scarcity_max_ratio: Optional[float] = Field(default=None, ge=0, le=1)


class ServiceCreate(BaseModel):
    name: str = Field(max_length=100)
    duration_minutes: int = Field(gt=0)
    price_kc: Optional[int] = None
