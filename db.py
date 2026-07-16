"""
db.py — PostgreSQL perzistence pro ApexSignal.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Optional

import psycopg2
import psycopg2.extras


def _get_dsn() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL není nastavená — appka bez ní nemůže nic uložit trvale. "
            "Na Renderu vytvoř PostgreSQL databázi a její Internal Database URL "
            "vlož jako env var DATABASE_URL webové službě (a restartuj ji)."
        )
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


@contextmanager
def get_cursor():
    """Context manager pro DB připojení."""
    conn = psycopg2.connect(_get_dsn(), cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tickets (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ticket_type VARCHAR(20) NOT NULL CHECK (ticket_type IN ('kratky', 'stredni', 'boost')),
    total_odds FLOAT NOT NULL,
    combined_probability FLOAT NOT NULL,
    recommended_stake_pct FLOAT NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',
    live_alert TEXT,
    actual_stake_amount FLOAT,
    actual_odds FLOAT,
    actual_profit_loss FLOAT DEFAULT 0,
    created_at TIMESTAMP DEFAULT now(),
    settled_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ticket_selections (
    id SERIAL PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    match_id INTEGER NOT NULL,
    home_team VARCHAR(255),
    away_team VARCHAR(255),
    market_type VARCHAR(50),
    selection VARCHAR(50),
    odds FLOAT NOT NULL,
    probability FLOAT,
    model_probability FLOAT,
    market_probability FLOAT,
    league VARCHAR(255),
    kickoff_date VARCHAR(50),
    reasoning TEXT,
    data_quality VARCHAR(50),
    result VARCHAR(10) DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS api_cache (
    cache_key VARCHAR(255) PRIMARY KEY,
    payload JSONB NOT NULL,
    expires_at TIMESTAMP NOT NULL
);
"""


def ensure_schema() -> None:
    """Vytvoří tabulky, pokud ještě neexistují."""
    with get_cursor() as cur:
        cur.execute(SCHEMA)
        
        # Migration: opravit CHECK constraint na ticket_type (přidat 'boost')
        try:
            # Nejdřív smazat VŠECHNY CHECK constraints na tickets
            # (neznáme jejich skutečné jméno na staré DB!)
            cur.execute("""
                SELECT constraint_name FROM information_schema.table_constraints 
                WHERE table_name = 'tickets' AND constraint_type = 'CHECK'
            """)
            constraints = cur.fetchall()
            for row in constraints:
                constraint_name = row['constraint_name']
                try:
                    cur.execute(f"ALTER TABLE tickets DROP CONSTRAINT IF EXISTS {constraint_name};")
                except Exception:
                    pass  # Constraint neexistuje nebo se nedá smazat
            
            # Teď přidat NOVÝ constraint s 'boost'
            cur.execute("""
                ALTER TABLE tickets 
                ADD CONSTRAINT tickets_ticket_type_check 
                CHECK (ticket_type IN ('kratky', 'stredni', 'boost'))
            """)
        except Exception as e:
            # Cokoliv selže, ignoruj (constraint možná již existuje správně)
            pass


def cache_get(key: str) -> Optional[list]:
    """Vrátí cachovaný payload z DB, pokud ještě nevypršel."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT payload FROM api_cache WHERE cache_key = %s AND expires_at > now()",
            (key,),
        )
        row = cur.fetchone()
        return row["payload"] if row else None


def cache_clear_all() -> int:
    """Smaže celou API cache."""
    with get_cursor() as cur:
        cur.execute("DELETE FROM api_cache")
        return cur.rowcount


def cache_set(key: str, payload: list, ttl_seconds: int = 4 * 3600) -> None:
    """Uloží payload do DB cache s TTL."""
    with get_cursor() as cur:
        cur.execute(
            """INSERT INTO api_cache (cache_key, payload, expires_at)
               VALUES (%s, %s::jsonb, now() + %s * interval '1 second')
               ON CONFLICT (cache_key) DO UPDATE
               SET payload = EXCLUDED.payload, expires_at = EXCLUDED.expires_at""",
            (key, json.dumps(payload), ttl_seconds),
        )


def create_user(email: str, password_hash: str) -> int:
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
            (email.strip().lower(), password_hash),
        )
        return cur.fetchone()["id"]


def get_user_by_email(email: str) -> Optional[dict]:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE email = %s", (email.strip().lower(),))
        return cur.fetchone()


def get_user_by_id(user_id: int) -> Optional[dict]:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        return cur.fetchone()


def insert_ticket(user_id: int, ticket) -> int:
    """ticket je objekt Ticket z probability_model"""
    with get_cursor() as cur:
        cur.execute(
            """INSERT INTO tickets (user_id, ticket_type, total_odds, combined_probability, recommended_stake_pct)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (user_id, ticket.ticket_type, ticket.total_odds, ticket.combined_probability, ticket.recommended_stake_pct),
        )
        ticket_id = cur.fetchone()["id"]
        for s in ticket.selections:
            cur.execute(
                """INSERT INTO ticket_selections
                   (ticket_id, match_id, home_team, away_team, market_type, selection, odds,
                    probability, model_probability, market_probability, league, kickoff_date,
                    reasoning, data_quality)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (ticket_id, s.match_id, s.home_team, s.away_team, s.market_type.value, s.selection, s.odds,
                 s.probability, s.model_probability, s.market_probability, s.league, s.kickoff_date,
                 s.reasoning, s.data_quality),
            )
    return ticket_id


def _row_to_dict(ticket_row: dict, selection_rows: list[dict]) -> dict:
    """Sestaví strukturu tiketu z DB řádků."""
    from probability_model import Ticket, SelectionCandidate, Sport, MarketType
    
    selections = [
        SelectionCandidate(
            match_id=sr["match_id"], home_team=sr["home_team"], away_team=sr["away_team"],
            sport=Sport.FOOTBALL, market_type=MarketType(sr["market_type"]), selection=sr["selection"],
            probability=float(sr["probability"]), odds=float(sr["odds"]),
            model_probability=float(sr["model_probability"]),
            market_probability=float(sr["market_probability"]) if sr["market_probability"] is not None else None,
            league=sr["league"] or "", kickoff_date=sr["kickoff_date"] or "",
            reasoning=sr["reasoning"] or "", data_quality=sr["data_quality"] or "",
        )
        for sr in selection_rows
    ]
    ticket_obj = Ticket(
        ticket_type=ticket_row["ticket_type"], selections=selections,
        total_odds=float(ticket_row["total_odds"]), combined_probability=float(ticket_row["combined_probability"]),
        recommended_stake_pct=float(ticket_row["recommended_stake_pct"]),
    )
    return {
        "ticket_id": ticket_row["id"],
        "ticket": ticket_obj,
        "status": ticket_row["status"],
        "live_alert": ticket_row["live_alert"],
        "actual_stake_amount": float(ticket_row["actual_stake_amount"]) if ticket_row["actual_stake_amount"] is not None else None,
        "actual_odds": float(ticket_row["actual_odds"]) if ticket_row["actual_odds"] is not None else None,
        "created_at": ticket_row.get("created_at"),
    }


def fetch_ticket_rows(user_id: Optional[int] = None, status: Optional[str] = None) -> list[dict]:
    """Vrátí tikety filtrované podle uživatele a/nebo stavu."""
    where, params = [], []
    if user_id is not None:
        where.append("user_id = %s"); params.append(user_id)
    if status is not None:
        where.append("status = %s"); params.append(status)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with get_cursor() as cur:
        cur.execute(f"SELECT * FROM tickets {where_sql} ORDER BY id", params)
        ticket_rows = cur.fetchall()
        result = []
        for trow in ticket_rows:
            cur.execute("SELECT * FROM ticket_selections WHERE ticket_id = %s ORDER BY id", (trow["id"],))
            sel_rows = cur.fetchall()
            row_dict = _row_to_dict(trow, sel_rows)
            row_dict["selections"] = [
                {
                    "id": sr["id"],
                    "match_id": sr["match_id"],
                    "market_type": sr["market_type"],
                    "selection": sr["selection"],
                    "odds": float(sr["odds"]),
                    "result": sr.get("result", "pending"),
                }
                for sr in sel_rows
            ]
            row_dict["total_odds"] = float(trow["total_odds"])
            row_dict["actual_profit_loss"] = float(trow["actual_profit_loss"]) if trow.get("actual_profit_loss") is not None else None
            result.append(row_dict)
    return result


def update_ticket_status(ticket_id: int, status: str) -> None:
    """Update ticket status."""
    with get_cursor() as cur:
        cur.execute("UPDATE tickets SET status = %s WHERE id = %s", (status, ticket_id))


def get_all_users() -> list[dict]:
    """Get all users."""
    with get_cursor() as cur:
        cur.execute("SELECT id as user_id, email FROM users")
        return cur.fetchall() or []


def delete_ticket(ticket_id: int) -> None:
    """Smaž tiket a všechny jeho selections."""
    with get_cursor() as cur:
        cur.execute("DELETE FROM ticket_selections WHERE ticket_id = %s", (ticket_id,))
        cur.execute("DELETE FROM tickets WHERE id = %s", (ticket_id,))


def update_selection_result(selection_id: int, result: str) -> None:
    """Update selection result."""
    with get_cursor() as cur:
        cur.execute("UPDATE ticket_selections SET result = %s WHERE id = %s", (result, selection_id))


def get_ticket_owner(ticket_id: int) -> Optional[int]:
    """Get ticket owner user_id."""
    with get_cursor() as cur:
        cur.execute("SELECT user_id FROM tickets WHERE id = %s", (ticket_id,))
        row = cur.fetchone()
        return row["user_id"] if row else None


def update_actual_stake(ticket_id: int, stake_amount: float, odds: float) -> bool:
    """Update actual stake."""
    with get_cursor() as cur:
        cur.execute(
            "UPDATE tickets SET actual_stake_amount = %s, actual_odds = %s WHERE id = %s RETURNING id",
            (stake_amount, odds, ticket_id),
        )
        return cur.fetchone() is not None


def update_live_alert(ticket_id: int, message: Optional[str]) -> None:
    """Update live alert."""
    with get_cursor() as cur:
        cur.execute("UPDATE tickets SET live_alert = %s WHERE id = %s", (message, ticket_id))


def set_ticket_status(ticket_id: int, status: str) -> None:
    """Set ticket status."""
    with get_cursor() as cur:
        cur.execute("UPDATE tickets SET status = %s WHERE id = %s", (status, ticket_id))


def set_live_alert(ticket_id: int, message: Optional[str]) -> None:
    """Set live alert."""
    with get_cursor() as cur:
        cur.execute("UPDATE tickets SET live_alert = %s WHERE id = %s", (message, ticket_id))
