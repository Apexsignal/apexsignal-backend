"""
db.py — PostgreSQL perzistence pro ApexSignal.

Appka se připojuje přes proměnnou prostředí DATABASE_URL (standardní
konvence Render/Heroku — na Renderu si vytvoř PostgreSQL databázi a její
"Internal Database URL" vlož jako env var DATABASE_URL webové službě).

Bez DATABASE_URL appka VYHODÍ VÝJIMKU, ne že by tiše spadla zpátky na
paměť procesu. To je záměr: appka dřív běžela na paměti, vypadalo to,
že track record a historie fungují, a první restart serveru (Render
free tier usíná po 15 min nečinnosti) všechno smazal beze stopy. Lepší
appce hned na startu nahlas řekni, že DB chybí, než aby si to "vyřešila"
mlčky a uživatel o ztracená data přišel, aniž by to zjistil.

Appka tady persistuje TIKETY (historie, track record, kalibrace, ROI,
reálné sázky) — to je to, co dává smysl uchovat napříč restarty. Live
signály (MomentumFilter stav, baseline kurzů, log pro jejich track
record) appka ZÁMĚRNĚ nechává v paměti i nadál — je to transientní stav
běžícího zápasu; jeho ztráta při restartu je tolerovatelná (appka jen
začne sledovat tlak znovu od nuly), na rozdíl od tiketové historie, kde
ztráta dat zničí celý smysl track recordu. Persistence live signálů je
dobrý další krok, ale samostatný — viz komentář u Repo v backend_api.py.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Optional

import psycopg2
import psycopg2.extras

from probability_model import Ticket, SelectionCandidate, Sport, MarketType


def _get_dsn() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL není nastavená — appka bez ní nemůže nic uložit trvale. "
            "Na Renderu vytvoř PostgreSQL databázi a její Internal Database URL "
            "vlož jako env var DATABASE_URL webové službě (a restartuj ji)."
        )
    # Render/Heroku historicky vrací schéma 'postgres://', novější psycopg2
    # chce 'postgresql://' — appka si to tiše opraví, ať appku nezdrží detail.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


@contextmanager
def get_cursor():
    """Otevře spojení, vrátí cursor s řádky jako dict (RealDictCursor), na konci commit/rollback a zavře spojení."""
    conn = psycopg2.connect(_get_dsn())
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              BIGSERIAL PRIMARY KEY,
    email           VARCHAR(255) UNIQUE NOT NULL,
    password_hash   VARCHAR(255) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_cache (
    cache_key       VARCHAR(255) PRIMARY KEY,
    payload         JSONB NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id                      BIGSERIAL PRIMARY KEY,
    user_id                 BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ticket_type             VARCHAR(20) NOT NULL CHECK (ticket_type IN ('safe','aggressive','kratky','stredni','dlouhy')),
    total_odds              NUMERIC(8,2) NOT NULL,
    combined_probability    NUMERIC(6,4) NOT NULL,
    recommended_stake_pct   NUMERIC(6,2) NOT NULL DEFAULT 0,
    status                  VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','won','lost')),
    live_alert              TEXT,
    actual_stake_amount     NUMERIC(12,2),
    actual_odds             NUMERIC(8,2),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at              TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);

CREATE TABLE IF NOT EXISTS ticket_selections (
    id                   BIGSERIAL PRIMARY KEY,
    ticket_id            BIGINT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    match_id             BIGINT NOT NULL,
    home_team            VARCHAR(120) NOT NULL,
    away_team            VARCHAR(120) NOT NULL,
    market_type          VARCHAR(50) NOT NULL,
    selection            VARCHAR(50) NOT NULL,
    odds                 NUMERIC(8,2) NOT NULL,
    probability          NUMERIC(6,4) NOT NULL,
    model_probability    NUMERIC(6,4) NOT NULL DEFAULT 0,
    market_probability   NUMERIC(6,4),
    league               VARCHAR(120) DEFAULT '',
    kickoff_date         VARCHAR(20) DEFAULT '',
    reasoning            TEXT DEFAULT '',
    data_quality         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_selections_ticket ON ticket_selections(ticket_id);
CREATE INDEX IF NOT EXISTS idx_selections_match ON ticket_selections(match_id);
"""


def ensure_schema() -> None:
    """Vytvoří tabulky, pokud ještě neexistují — bezpečné volat opakovaně při každém startu appky."""
    with get_cursor() as cur:
        cur.execute(SCHEMA)
        # Migrace — přidání result sloupce do ticket_selections
        cur.execute("""
            DO $$
            BEGIN
                ALTER TABLE ticket_selections ADD COLUMN IF NOT EXISTS result VARCHAR(10) DEFAULT 'pending';
            EXCEPTION WHEN others THEN NULL;
            END $$;
        """)
        # Appka to udělá bezpečně: smaže starý constraint a přidá nový.
        cur.execute("""
            DO $$
            BEGIN
                ALTER TABLE tickets DROP CONSTRAINT IF EXISTS tickets_ticket_type_check;
                ALTER TABLE tickets ADD CONSTRAINT tickets_ticket_type_check
                    CHECK (ticket_type IN ('safe','aggressive','kratky','stredni','dlouhy'));
            EXCEPTION WHEN others THEN
                NULL; -- tabulka ještě neexistuje, ignoruj
            END $$;
        """)


def cache_get(key: str) -> Optional[list]:
    """Vrátí cachovaný payload z DB, pokud ještě nevypršel. Jinak None."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT payload FROM api_cache WHERE cache_key = %s AND expires_at > now()",
            (key,),
        )
        row = cur.fetchone()
        return row["payload"] if row else None


def cache_clear_all() -> int:
    """Smaže celou API cache — použij po opravách, kdy se změnil formát dat."""
    with get_cursor() as cur:
        cur.execute("DELETE FROM api_cache")
        return cur.rowcount


def cache_set(key: str, payload: list, ttl_seconds: int = 4 * 3600) -> None:
    """Uloží payload do DB cache s TTL. Přepíše existující záznam (upsert)."""
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


def insert_ticket(user_id: int, ticket: Ticket) -> int:
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
    """
    Sestaví ze syrových DB řádků přesně tu strukturu, kterou Repo dřív
    drželo v paměti — {"ticket_id":, "ticket": Ticket(...), "status":,
    "live_alert":, "actual_stake_amount":, "actual_odds":}. Díky tomu
    appka nemusí přepisovat ŽÁDNOU z agregačních funkcí (track record,
    kalibrace, ROI) — ty dál dostávají stejný tvar dat, jen z databáze.
    """
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
    }


def fetch_ticket_rows(user_id: Optional[int] = None, status: Optional[str] = None) -> list[dict]:
    """Vrátí tikety (+ jejich výběry), volitelně filtrované podle uživatele a/nebo stavu."""
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
            result.append(_row_to_dict(trow, sel_rows))
    return result


def delete_ticket(ticket_id: int) -> None:
    with get_cursor() as cur:
        cur.execute("DELETE FROM tickets WHERE id = %s", (ticket_id,))


def get_selection_owner(selection_id: int) -> Optional[int]:
    """Vrátí user_id vlastníka výběru přes jeho tiket."""
    with get_cursor() as cur:
        cur.execute("""
            SELECT t.user_id FROM ticket_selections ts
            JOIN tickets t ON t.id = ts.ticket_id
            WHERE ts.id = %s
        """, (selection_id,))
        row = cur.fetchone()
        return row["user_id"] if row else None


def update_selection_odds(selection_id: int, odds: float) -> None:
    with get_cursor() as cur:
        cur.execute("UPDATE ticket_selections SET odds = %s WHERE id = %s", (odds, selection_id))


def update_selection_result(selection_id: int, result: str) -> None:
    with get_cursor() as cur:
        cur.execute("UPDATE ticket_selections SET result = %s WHERE id = %s", (result, selection_id))


def get_ticket_owner(ticket_id: int) -> Optional[int]:
    """Vrátí user_id vlastníka tiketu, nebo None, pokud tiket neexistuje — appka to používá k ověření, že si uživatel nenastavuje sázku na cizí tiket."""
    with get_cursor() as cur:
        cur.execute("SELECT user_id FROM tickets WHERE id = %s", (ticket_id,))
        row = cur.fetchone()
        return row["user_id"] if row else None


def update_ticket_profit_loss(ticket_id: int, stake: float, odds: float, status: str) -> None:
    """Spočítá a uloží zisk/ztrátu po vyhodnocení tiketu."""
    profit_loss = round(stake * odds - stake, 2) if status == "won" else round(-stake, 2)
    with get_cursor() as cur:
        cur.execute(
            "UPDATE tickets SET actual_profit_loss = %s WHERE id = %s",
            (profit_loss, ticket_id),
        )


def update_ticket_status(ticket_id: int, status: str) -> None:
    with get_cursor() as cur:
        cur.execute(
            """UPDATE tickets SET status = %s,
                   settled_at = CASE WHEN %s IN ('won','lost') THEN now() ELSE settled_at END
               WHERE id = %s""",
            (status, status, ticket_id),
        )


def update_live_alert(ticket_id: int, message: Optional[str]) -> None:
    with get_cursor() as cur:
        cur.execute("UPDATE tickets SET live_alert = %s WHERE id = %s", (message, ticket_id))


def update_actual_stake(ticket_id: int, stake_amount: float, odds: float) -> bool:
    with get_cursor() as cur:
        cur.execute(
            "UPDATE tickets SET actual_stake_amount = %s, actual_odds = %s WHERE id = %s RETURNING id",
            (stake_amount, odds, ticket_id),
        )
        return cur.fetchone() is not None
