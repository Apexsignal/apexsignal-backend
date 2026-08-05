"""
db.py — PostgreSQL perzistence pro rezervační systém masáží.

Styl schválně kopíruje ApexSignal `db.py` (connection pool + get_cursor()
context manager + CREATE TABLE IF NOT EXISTS schéma) — jiná appka, ale
stejná osvědčená kostra, ať se v tom čte snadno i pro toho, kdo zná
ten druhý repozitář.
"""
from __future__ import annotations

import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

from .config import settings

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            1, 10, settings.database_url, cursor_factory=psycopg2.extras.RealDictCursor
        )
    return _pool


@contextmanager
def get_cursor():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        pool.putconn(conn)


SCHEMA = """
CREATE TABLE IF NOT EXISTS masseuses (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    phone VARCHAR(30),
    active BOOLEAN NOT NULL DEFAULT true,
    scarcity_min_ratio NUMERIC(3,2) NOT NULL DEFAULT 0.2,
    scarcity_max_ratio NUMERIC(3,2) NOT NULL DEFAULT 0.4,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS services (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    duration_minutes INTEGER NOT NULL,
    price_kc INTEGER,
    active BOOLEAN NOT NULL DEFAULT true
);

-- Týdenní opakující se pracovní doba (šablona). Jedna masérka může mít
-- víc řádků na stejný den (rozdělená směna).
CREATE TABLE IF NOT EXISTS working_hours (
    id SERIAL PRIMARY KEY,
    masseuse_id INTEGER NOT NULL REFERENCES masseuses(id) ON DELETE CASCADE,
    weekday SMALLINT NOT NULL CHECK (weekday BETWEEN 0 AND 6),  -- 0=pondělí .. 6=neděle
    start_time TIME NOT NULL,
    end_time TIME NOT NULL
);

-- Výjimka pro konkrétní datum — přebije týdenní šablonu (jiná hodina
-- v ten den, nebo úplné volno).
CREATE TABLE IF NOT EXISTS schedule_overrides (
    id SERIAL PRIMARY KEY,
    masseuse_id INTEGER NOT NULL REFERENCES masseuses(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    day_off BOOLEAN NOT NULL DEFAULT false,
    start_time TIME,
    end_time TIME,
    UNIQUE (masseuse_id, date)
);

-- Ruční blokace konkrétního časového rozsahu (dovolená, pauza, soukromý
-- důvod) — nastavuje admin/masérka, nezávisle na rezervacích klientů.
CREATE TABLE IF NOT EXISTS blocked_slots (
    id SERIAL PRIMARY KEY,
    masseuse_id INTEGER NOT NULL REFERENCES masseuses(id) ON DELETE CASCADE,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ NOT NULL,
    reason VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bookings (
    id SERIAL PRIMARY KEY,
    masseuse_id INTEGER NOT NULL REFERENCES masseuses(id),
    service_id INTEGER NOT NULL REFERENCES services(id),
    slot_start TIMESTAMPTZ NOT NULL,
    slot_end TIMESTAMPTZ NOT NULL,
    client_name VARCHAR(100),
    client_phone VARCHAR(30) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending, confirmed, expired, cancelled
    confirm_token VARCHAR(64) UNIQUE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    confirmed_at TIMESTAMPTZ
);

-- Jeden aktivní (pending/confirmed) záznam na masérku+termín — brání
-- dvěma klientům rezervovat stejný slot souběžně (appka na tohle
-- spoléhá při ukládání, ne jen na kontrolu v Pythonu předem).
CREATE UNIQUE INDEX IF NOT EXISTS uq_bookings_active_slot
    ON bookings (masseuse_id, slot_start)
    WHERE status IN ('pending', 'confirmed');

CREATE INDEX IF NOT EXISTS idx_bookings_status_expires
    ON bookings (status, expires_at);
"""


def init_db() -> None:
    with get_cursor() as cur:
        cur.execute(SCHEMA)


def generate_confirm_token() -> str:
    return secrets.token_urlsafe(32)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
