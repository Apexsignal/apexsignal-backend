"""
db.py — PostgreSQL perzistence pro ApexSignal.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime
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
    ticket_type VARCHAR(20) NOT NULL,
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
    kickoff_time VARCHAR(50),
    country VARCHAR(255),
    reasoning TEXT,
    data_quality VARCHAR(50),
    result VARCHAR(10) DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS api_cache (
    cache_key VARCHAR(255) PRIMARY KEY,
    payload JSONB NOT NULL,
    expires_at TIMESTAMP NOT NULL
);

-- Tokenový systém (viz ApexSignal – Tokenomika & Tokenový Model). Stripe
-- napojení přijde v dalším kroku — tahle vrstva (zůstatek, transakce,
-- kódy na uplatnění) funguje nezávisle na tom, odkud tokeny přišly.
CREATE TABLE IF NOT EXISTS user_tokens (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    balance INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS token_transactions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount INTEGER NOT NULL,          -- kladné = příjem (kód, dokup), záporné = útrata (vygenerování tiketu)
    reason VARCHAR(100) NOT NULL,     -- např. 'UNLOCK_KRATKY', 'REDEEM_CODE:ABC123'
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS redeem_codes (
    code VARCHAR(64) PRIMARY KEY,
    tokens INTEGER NOT NULL,
    max_uses INTEGER NOT NULL DEFAULT 1,
    uses_count INTEGER NOT NULL DEFAULT 0,
    expires_at TIMESTAMP,
    note VARCHAR(255),
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS redeem_code_uses (
    code VARCHAR(64) NOT NULL REFERENCES redeem_codes(code) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    used_at TIMESTAMP DEFAULT now(),
    PRIMARY KEY (code, user_id)
);

CREATE TABLE IF NOT EXISTS stripe_events (
    event_id VARCHAR(255) PRIMARY KEY,
    processed_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    token VARCHAR(64) PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMP NOT NULL,
    used BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMP DEFAULT now()
);
"""


def ensure_schema() -> None:
    """Vytvoří tabulky, pokud ještě neexistují."""
    with get_cursor() as cur:
        cur.execute(SCHEMA)

    # DŮLEŽITÉ: každá kompatibilitní úprava níže běží ve VLASTNÍ transakci
    # (vlastní get_cursor() blok), ne ve stejné transakci jako SCHEMA výše.
    # Dřív byly všechny v JEDNÉ transakci — jakmile "ALTER TABLE ... ADD
    # COLUMN" spadl (protože sloupec už existoval, ob obvyklý stav na
    # produkci po prvním úspěšném přidání), Postgres tím celou transakci
    # označí jako "aborted". Try/except kolem cur.execute() sice zachytí
    # PYTHONOVOU výjimku, ale SQL transakce zůstane otrávená — a finální
    # conn.commit() na konci get_cursor() bloku pak TICHO (bez chyby)
    # celou transakci rollbackne, včetně předtím úspěšně provedeného
    # cur.execute(SCHEMA)! Nové tabulky (CREATE TABLE IF NOT EXISTS) se
    # tak nikdy reálně neuložily — při každém restartu appka "úspěšně"
    # odešla z ensure_schema(), ale v DB nic nepřibylo.
    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE tickets DROP CONSTRAINT IF EXISTS tickets_ticket_type_check")
    except Exception:
        pass  # Constraint neexistuje nebo se nedá smazat, ignoruj

    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE ticket_selections ADD COLUMN IF NOT EXISTS kickoff_time VARCHAR(50)")
    except Exception:
        pass

    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE ticket_selections ADD COLUMN IF NOT EXISTS country VARCHAR(255)")
    except Exception:
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


def has_transaction_with_reason(user_id: int, reason: str) -> bool:
    """Appka tohle používá jako pojistku proti dvojité refundaci stejné platby."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM token_transactions WHERE user_id = %s AND reason = %s LIMIT 1",
            (user_id, reason),
        )
        return cur.fetchone() is not None


def get_stripe_payments_for_user(user_id: int) -> list[dict]:
    """Appka odsud bere seznam Stripe plateb konkrétního uživatele pro
    podporu (reklamace/refundace) — appka platby pozná podle reason
    prefixu 'STRIPE_PAYMENT:', za dvojtečkou je Stripe Checkout session ID."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, amount, reason, created_at FROM token_transactions
            WHERE user_id = %s AND reason LIKE 'STRIPE_PAYMENT:%%'
            ORDER BY created_at DESC
            """,
            (user_id,),
        )
        rows = cur.fetchall()
        return [
            {
                "transaction_id": r["id"],
                "tokens": r["amount"],
                "session_id": r["reason"].split(":", 1)[1],
                "created_at": r["created_at"],
            }
            for r in rows
        ]


def get_conversion_funnel(days: int = 30) -> dict:
    """
    Appka appce ukazuje, kde lidi ubývají mezi registrací a placením:
    kolik se jich zaregistrovalo, kolik z nich appce uložilo aspoň jeden
    tiket (appka to bere jako 'reálně appku vyzkoušeli'), a kolik z nich
    appce aspoň jednou zaplatilo. `days` appka omezí jen na nedávné
    registrace, ať appka appce neukazuje historicky zkreslené číslo
    kombinující starý i nový provoz appky.
    """
    with get_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM users WHERE created_at > now() - %s * interval '1 day'",
            (days,),
        )
        registered = cur.fetchone()["n"]

        cur.execute(
            """
            SELECT COUNT(DISTINCT u.id) AS n FROM users u
            JOIN tickets t ON t.user_id = u.id
            WHERE u.created_at > now() - %s * interval '1 day'
            """,
            (days,),
        )
        saved_ticket = cur.fetchone()["n"]

        cur.execute(
            """
            SELECT COUNT(DISTINCT u.id) AS n FROM users u
            JOIN token_transactions tt ON tt.user_id = u.id
            WHERE u.created_at > now() - %s * interval '1 day'
              AND tt.reason LIKE 'STRIPE_PAYMENT:%%'
            """,
            (days,),
        )
        paid = cur.fetchone()["n"]

        return {
            "period_days": days,
            "registered": registered,
            "saved_first_ticket": saved_ticket,
            "paid": paid,
        }


def get_user_by_id(user_id: int) -> Optional[dict]:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        return cur.fetchone()


def update_password_hash(user_id: int, password_hash: str) -> None:
    with get_cursor() as cur:
        cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id))


def delete_user(user_id: int) -> None:
    """
    Smaže účet i všechna navázaná data — tickets, ticket_selections,
    user_tokens, token_transactions, redeem_code_uses i
    password_reset_tokens appka smaže automaticky přes ON DELETE CASCADE
    (viz cizí klíče v SCHEMA výše), appka tu maže jen samotný řádek
    users.
    """
    with get_cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))


def create_password_reset_token(token: str, user_id: int, expires_at) -> None:
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO password_reset_tokens (token, user_id, expires_at) VALUES (%s, %s, %s)",
            (token, user_id, expires_at),
        )


def consume_password_reset_token(token: str) -> Optional[int]:
    """
    Ověří token (existuje, nevypršel, ještě nebyl použitý) a rovnou ho
    appka označí jako použitý — appka to dělá v jedné transakci se
    zamčením řádku (FOR UPDATE), ať nejde stejný token uplatnit dvakrát
    souběžně. Vrátí user_id, nebo None, když token neplatí.
    """
    with get_cursor() as cur:
        cur.execute("SELECT * FROM password_reset_tokens WHERE token = %s FOR UPDATE", (token,))
        row = cur.fetchone()
        if row is None or row["used"] or row["expires_at"] < datetime.now():
            return None
        cur.execute("UPDATE password_reset_tokens SET used = true WHERE token = %s", (token,))
        return row["user_id"]


def has_ticket_since(user_id: int, ticket_type: str, since) -> bool:
    """Appka tohle používá jako pojistku proti duplicitnímu spuštění
    denní automatiky (viz /admin/daily-tickets) — když appku někdo/něco
    spustí 2x za sebou (retry po timeoutu na klientovi, zatímco server
    první běh dál dokončuje na pozadí), druhé spuštění tenhle typ tiketu
    přeskočí, místo aby appka vygenerovala a poslala duplicitní tiket."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM tickets WHERE user_id = %s AND ticket_type = %s AND created_at >= %s LIMIT 1",
            (user_id, ticket_type, since),
        )
        return cur.fetchone() is not None


def count_tickets_since(user_id: int, ticket_type: str, since) -> int:
    """Appka tohle používá pro denní automatiku, co má za den vygenerovat
    VÍC tiketů stejného typu (viz /admin/daily-tickets) — na rozdíl od
    has_ticket_since appka nechce blokovat po prvním, jen zjistit kolik
    už jich dnes je, aby dogenerovala jen chybějící počet."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM tickets WHERE user_id = %s AND ticket_type = %s AND created_at >= %s",
            (user_id, ticket_type, since),
        )
        return cur.fetchone()["n"]


def insert_ticket(user_id: int, ticket, created_at=None) -> int:
    """ticket je objekt Ticket z probability_model. created_at appka nastaví
    jen výjimečně (viz /admin/showcase/seed — appka tam ručně přidává
    STARŠÍ vyhrané tikety a chce appce zachovat jejich reálné datum, ne
    now())."""
    # Validace - ticket_type musí být povolený typ
    allowed_types = {'kratky', 'stredni', 'boost'}
    if ticket.ticket_type not in allowed_types:
        raise ValueError(f"Invalid ticket_type: {ticket.ticket_type}. Allowed: {allowed_types}")

    with get_cursor() as cur:
        if created_at is not None:
            cur.execute(
                """INSERT INTO tickets (user_id, ticket_type, total_odds, combined_probability, recommended_stake_pct, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (user_id, ticket.ticket_type, ticket.total_odds, ticket.combined_probability, ticket.recommended_stake_pct, created_at),
            )
        else:
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
                    probability, model_probability, market_probability, league, kickoff_date, kickoff_time, country,
                    reasoning, data_quality)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (ticket_id, s.match_id, s.home_team, s.away_team, s.market_type.value, s.selection, s.odds,
                 s.probability, s.model_probability, s.market_probability, s.league, s.kickoff_date, s.kickoff_time, s.country,
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
            league=sr["league"] or "", kickoff_date=sr["kickoff_date"] or "", kickoff_time=sr.get("kickoff_time") or "", country=sr.get("country") or "",
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


def fetch_ticket_rows(user_id: Optional[int] = None, status: Optional[str] = None, ticket_id: Optional[int] = None) -> list[dict]:
    """Vrátí tikety filtrované podle uživatele, stavu a/nebo ticket ID."""
    where, params = [], []
    if user_id is not None:
        where.append("user_id = %s"); params.append(user_id)
    if status is not None:
        where.append("status = %s"); params.append(status)
    if ticket_id is not None:
        where.append("id = %s"); params.append(ticket_id)
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


def update_ticket_fields(ticket_id: int, total_odds: Optional[float] = None, actual_stake_amount: Optional[float] = None) -> None:
    """Update jen těch polí tiketu, co appka skutečně dostala (viz PATCH /tickets/{id})."""
    updates, params = [], []
    if total_odds is not None:
        updates.append("total_odds = %s")
        params.append(total_odds)
    if actual_stake_amount is not None:
        updates.append("actual_stake_amount = %s")
        params.append(actual_stake_amount)
    if not updates:
        return
    params.append(ticket_id)
    with get_cursor() as cur:
        cur.execute(f"UPDATE tickets SET {', '.join(updates)} WHERE id = %s", params)


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


def delete_selection(ticket_id: int, selection_index: int) -> bool:
    """Smaže selection ze tiketu podle indexu. Vrátí True pokud byl smazán celý tiket."""
    with get_cursor() as cur:
        # Najdi všechny selections pro tiket, seřazené podle ID
        cur.execute("SELECT id FROM ticket_selections WHERE ticket_id = %s ORDER BY id", (ticket_id,))
        selection_rows = cur.fetchall()
        
        # Pokud index existuje - smaž ho
        if selection_index < len(selection_rows):
            selection_id = selection_rows[selection_index]["id"]
            cur.execute("DELETE FROM ticket_selections WHERE id = %s", (selection_id,))
            
            # Přepočítej total_odds (vezmi zbylé selections)
            cur.execute("SELECT odds FROM ticket_selections WHERE ticket_id = %s ORDER BY id", (ticket_id,))
            remaining = cur.fetchall()
            if remaining:
                new_odds = 1.0
                for row in remaining:
                    new_odds *= float(row["odds"])
                cur.execute("UPDATE tickets SET total_odds = %s WHERE id = %s", (round(new_odds, 2), ticket_id))
                return False  # Tiket pořád existuje
            else:
                # Poslední selection - smaž tiket
                cur.execute("DELETE FROM tickets WHERE id = %s", (ticket_id,))
                return True  # Tiket byl smazán

        return False


# =====================================================================
# Tokenový systém
# =====================================================================
def get_token_balance(user_id: int) -> int:
    with get_cursor() as cur:
        cur.execute("SELECT balance FROM user_tokens WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return row["balance"] if row else 0


def adjust_tokens(user_id: int, amount: int, reason: str) -> int:
    """
    Přičte/odečte tokeny (amount může být záporné) a zaloguje transakci —
    appka appka obojí dělá ve STEJNÉ transakci (get_cursor commituje na
    konci celého bloku), ať zůstatek a log nikdy nerozjedou. Vrací NOVÝ
    zůstatek. Nekontroluje, jestli je výsledek záporný — to musí appka
    ověřit PŘED zavoláním (viz has_enough_tokens), ať se dá odlišit
    "nedostatek tokenů" od jiné chyby.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_tokens (user_id, balance, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (user_id) DO UPDATE
                SET balance = user_tokens.balance + EXCLUDED.balance, updated_at = now()
            RETURNING balance
            """,
            (user_id, amount),
        )
        new_balance = cur.fetchone()["balance"]
        cur.execute(
            "INSERT INTO token_transactions (user_id, amount, reason) VALUES (%s, %s, %s)",
            (user_id, amount, reason),
        )
        return new_balance


def mark_stripe_event_if_new(event_id: str) -> bool:
    """
    Stripe může kvůli chybějícímu/pomalému 200 OK doručit stejnou webhook
    událost víckrát — appka si eventy pamatuje, ať tokeny nepřipíše
    2x za jednu platbu. Vrací True jen když je to POPRVÉ (appka je má
    zpracovat), False při duplicitním doručení.
    """
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO stripe_events (event_id) VALUES (%s) ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
            (event_id,),
        )
        return cur.fetchone() is not None


def create_redeem_code(code: str, tokens: int, max_uses: int = 1, expires_at=None, note: str = "") -> None:
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO redeem_codes (code, tokens, max_uses, expires_at, note) VALUES (%s, %s, %s, %s, %s)",
            (code, tokens, max_uses, expires_at, note),
        )


def redeem_code(code: str, user_id: int) -> dict:
    """
    Uplatní kód pro daného uživatele. Appka v jedné DB transakci: zamkne
    řádek kódu (FOR UPDATE, ať appka neuplatní stejný kód 2x souběžně nad
    limit), ověří platnost/limit/že ho tenhle uživatel ještě nepoužil,
    připíše tokeny a zaloguje použití. Vrací {"ok": True, "tokens": N,
    "balance": N} nebo {"ok": False, "error": "..."}.
    """
    with get_cursor() as cur:
        cur.execute("SELECT * FROM redeem_codes WHERE code = %s FOR UPDATE", (code,))
        row = cur.fetchone()
        if row is None:
            return {"ok": False, "error": "Kód neexistuje"}
        if row["expires_at"] is not None and row["expires_at"] < datetime.now():
            return {"ok": False, "error": "Kódu vypršela platnost"}
        if row["uses_count"] >= row["max_uses"]:
            return {"ok": False, "error": "Kód už byl vyčerpán"}

        cur.execute("SELECT 1 FROM redeem_code_uses WHERE code = %s AND user_id = %s", (code, user_id))
        if cur.fetchone() is not None:
            return {"ok": False, "error": "Tenhle kód jsi už uplatnil"}

        cur.execute("UPDATE redeem_codes SET uses_count = uses_count + 1 WHERE code = %s", (code,))
        cur.execute("INSERT INTO redeem_code_uses (code, user_id) VALUES (%s, %s)", (code, user_id))

        cur.execute(
            """
            INSERT INTO user_tokens (user_id, balance, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (user_id) DO UPDATE
                SET balance = user_tokens.balance + EXCLUDED.balance, updated_at = now()
            RETURNING balance
            """,
            (user_id, row["tokens"]),
        )
        new_balance = cur.fetchone()["balance"]
        cur.execute(
            "INSERT INTO token_transactions (user_id, amount, reason) VALUES (%s, %s, %s)",
            (user_id, row["tokens"], f"REDEEM_CODE:{code}"),
        )
        return {"ok": True, "tokens": row["tokens"], "balance": new_balance}
