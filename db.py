"""
db.py — PostgreSQL perzistence pro ApexSignal.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool


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


# Appka dřív otvírala NOVÉ TCP připojení k Postgresu na úplně KAŽDÝ dotaz
# (get_cursor() volalo psycopg2.connect() pokaždé znovu) — u Historie,
# co appka souběžně vyhodnocuje desítky výběrů najednou (viz
# _try_settle_ticket v backend_api.py), to znamenalo klidně 30-60 nových
# připojení najednou jen pro jedno otevření Historie. Pool appce dovolí
# připojení znovupoužívat mezi requesty.
_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        # maxconn 40: appka umí souběžně vyhodnocovat až 4 tikety × 8 výběrů
        # (viz SETTLE_LEG_WORKERS v backend_api.py) = špička 32 souběžných
        # připojení jen z jednoho requestu na Historii, plus rezerva pro
        # ostatní současné requesty.
        _pool = psycopg2.pool.ThreadedConnectionPool(
            2, 40, _get_dsn(), cursor_factory=psycopg2.extras.RealDictCursor
        )
    return _pool


@contextmanager
def get_cursor():
    """Context manager pro DB připojení — bere/vrací spojení z poolu místo navazování nového."""
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

-- Lidé, co appce napsali /start na Telegramu — appka jim pak denně
-- posílá 1 kratky + 1 stredni tiket (a v pátek navíc boost), viz
-- run_daily_tickets. chat_id appka zjistí automaticky z webhooku,
-- žádné ruční dohledávání přes getUpdates.
CREATE TABLE IF NOT EXISTS telegram_subscribers (
    chat_id BIGINT PRIMARY KEY,
    first_name VARCHAR(255),
    active BOOLEAN NOT NULL DEFAULT true,
    joined_at TIMESTAMP DEFAULT now()
);

-- Předplatné placeného Telegram kanálu a jeho párovací kódy appka
-- zakládá až v migračním bloku níž (ensure_schema) — potřebují appce
-- zjistit, jestli na produkci ještě běží STARÁ struktura (vázaná na
-- user_id appky), a tu appka nejdřív zahodit. Viz komentář tam.
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

    # telegram_subscribers vzniklo dřív než placený kanál, takže tam vazba
    # na uživatele chybí — appka ji doplňuje tady. Sloupec je NULLABLE
    # schválně: řádky z doby před zámkem (kdy /start stačilo k odběru)
    # zůstanou nespárované, a protože rozesílka platících dělá JOIN přes
    # user_id, žádný z nich placené tikety nedostane.
    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE telegram_subscribers ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
    except Exception:
        pass

    try:
        with get_cursor() as cur:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_telegram_subscribers_user ON telegram_subscribers (user_id)")
    except Exception:
        pass

    # Předplatné placeného kanálu appka zpočátku vázala na user_id appky
    # (musel jsi mít účet a být přihlášený, aby ses spároval). Appka to
    # přepracovala na model BEZ ÚČTU — zákazník platí přes samostatný
    # Stripe Payment Link (žádné přihlášení, jen e-mail, který zadá do
    # Stripe checkoutu) a appka mu pak e-mailem pošle párovací odkaz na
    # Telegram. Obě tabulky appka nikdy reálně nepoužila (nikdo si přes
    # ně nezaplatil), takže je bezpečné je zahodit a založit znovu s
    # novou strukturou (klíč je teď e-mail/Stripe ID, ne user_id) místo
    # migrace neexistujících dat.
    try:
        with get_cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS telegram_link_codes")
    except Exception:
        pass

    try:
        with get_cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS subscriptions")
    except Exception:
        pass

    try:
        with get_cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id                     BIGSERIAL PRIMARY KEY,
                    email                  VARCHAR(255) NOT NULL,
                    stripe_customer_id     VARCHAR(255),
                    stripe_subscription_id VARCHAR(255) UNIQUE,
                    status                 VARCHAR(32) NOT NULL DEFAULT 'inactive',
                    current_period_end     TIMESTAMPTZ,
                    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
    except Exception:
        pass

    try:
        with get_cursor() as cur:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_stripe_sub ON subscriptions (stripe_subscription_id)")
    except Exception:
        pass

    try:
        with get_cursor() as cur:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_email ON subscriptions (email)")
    except Exception:
        pass

    # Jednorázový kód, kterým appka propojí Telegram s předplatným po
    # zaplacení — appka ho vygeneruje a e-mailem pošle SAMA hned po
    # úspěšné platbě, protože na rozdíl od dřívějška tu není žádný
    # přihlášený uživatel, který by si o kód mohl požádat sám.
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_link_codes (
                    code            VARCHAR(32) PRIMARY KEY,
                    subscription_id BIGINT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                    expires_at      TIMESTAMPTZ NOT NULL,
                    used_at         TIMESTAMPTZ,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
    except Exception:
        pass

    # telegram_subscribers appka teď páruje přes subscription_id (nová
    # cesta, bez účtu) — user_id appka nechává být pro případný budoucí
    # návrat k appce-vázanému modelu, nepoužitý sloupec nic nerozbíjí.
    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE telegram_subscribers ADD COLUMN IF NOT EXISTS subscription_id BIGINT REFERENCES subscriptions(id) ON DELETE CASCADE")
    except Exception:
        pass

    try:
        with get_cursor() as cur:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_telegram_subscribers_subscription ON telegram_subscribers (subscription_id)")
    except Exception:
        pass

    # Neomezené generování (4990 Kč/měsíc, viz _require_generation_enabled
    # a _check_daily_generation_cap) — unlimited_until appka nechává NULL,
    # dokud si uživatel tarif nekoupí; denni_generations_count/date appka
    # počítá pokusy o generování, ne uložené tikety (appka platí za pokus,
    # i kdyby uživatel výsledek nakonec neuložil).
    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS unlimited_until TIMESTAMPTZ")
    except Exception:
        pass

    # Kódy appka dřív uměly připsat jen tokeny — teď appka umí kódem
    # odemknout i neomezené generování na N dní (viz redeem_code níže).
    # unlimited_days appka nechává NULL/0 u čistě tokenových kódů.
    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE redeem_codes ADD COLUMN IF NOT EXISTS unlimited_days INTEGER")
    except Exception:
        pass

    # Stripe customer ID appka potřebuje, aby appka uměla otevřít billing
    # portál (zrušení/změna karty) — bez něj appka nemá jak dohledat, který
    # Stripe zákazník k danému appky účtu patří.
    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS unlimited_stripe_customer_id TEXT")
    except Exception:
        pass

    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_generations_count INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass

    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_generations_date DATE")
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
    Appka tímhle ukazuje, kde lidi ubývají mezi registrací a placením:
    kolik se jich zaregistrovalo, kolik z nich appce uložilo aspoň jeden
    tiket (appka to bere jako 'reálně appku vyzkoušeli'), a kolik z nich
    appce aspoň jednou zaplatilo. `days` appka omezí jen na nedávné
    registrace, ať appka neukazuje historicky zkreslené číslo
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

        # Appka dřív natahovala selections JEDNÍM dotazem PRO KAŽDÝ tiket
        # zvlášť (N+1) — appka místo toho natáhne všechny naráz jedním
        # dotazem a rozdělí je v Pythonu podle ticket_id. U historie
        # s desítkami tiketů to appce ušetří desítky zbytečných DB
        # roundtripů na každé volání.
        ticket_ids = [trow["id"] for trow in ticket_rows]
        sel_by_ticket: dict[int, list[dict]] = {tid: [] for tid in ticket_ids}
        if ticket_ids:
            cur.execute(
                "SELECT * FROM ticket_selections WHERE ticket_id = ANY(%s) ORDER BY ticket_id, id",
                (ticket_ids,),
            )
            for sr in cur.fetchall():
                sel_by_ticket[sr["ticket_id"]].append(sr)

        result = []
        for trow in ticket_rows:
            sel_rows = sel_by_ticket[trow["id"]]
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


def get_selection_owner(selection_id: int) -> Optional[int]:
    """Get user_id vlastnící tiket, pod který tenhle výběr patří.

    backend_api.py (/selections/{id}/odds, /selections/{id}/result) tuhle
    funkci volal, ale v db.py nikdy neexistovala — obě volání appka
    zjistila v auditu, byla vždy shozena AttributeErrorem (bez try/except
    kolem, appka viditelně vracela 500, ne tichou chybu)."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT t.user_id FROM ticket_selections s "
            "JOIN tickets t ON t.id = s.ticket_id WHERE s.id = %s",
            (selection_id,),
        )
        row = cur.fetchone()
        return row["user_id"] if row else None


def update_selection_odds(selection_id: int, odds: float) -> None:
    """Update selection odds."""
    with get_cursor() as cur:
        cur.execute("UPDATE ticket_selections SET odds = %s WHERE id = %s", (odds, selection_id))


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
    appka obojí dělá ve STEJNÉ transakci (get_cursor commituje na konci
    celého bloku), ať zůstatek a log nikdy nerozjedou. Vrací NOVÝ
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


# =====================================================================
# Neomezené generování (4990 Kč/měsíc, strop 10 generování/den)
# =====================================================================
def get_unlimited_until(user_id: int) -> Optional[datetime]:
    with get_cursor() as cur:
        cur.execute("SELECT unlimited_until FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return row["unlimited_until"] if row else None


def set_unlimited_until(user_id: int, until: datetime, stripe_customer_id: Optional[str] = None) -> None:
    with get_cursor() as cur:
        if stripe_customer_id is not None:
            cur.execute(
                "UPDATE users SET unlimited_until = %s, unlimited_stripe_customer_id = %s WHERE id = %s",
                (until, stripe_customer_id, user_id),
            )
        else:
            cur.execute("UPDATE users SET unlimited_until = %s WHERE id = %s", (until, user_id))


def get_unlimited_stripe_customer_id(user_id: int) -> Optional[str]:
    with get_cursor() as cur:
        cur.execute("SELECT unlimited_stripe_customer_id FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return row["unlimited_stripe_customer_id"] if row else None


def increment_daily_generation_count(user_id: int) -> int:
    """
    Appka tady atomicky (jeden UPDATE, ne SELECT+UPDATE) buď nastartuje
    nový den na 1, nebo připočte k dnešnímu počtu — CASE běží uvnitř
    databáze, takže dva souběžné požadavky appku nepřipraví o jeden
    přírůstek (na rozdíl od "appka si přečte počet v Pythonu, přičte 1,
    zapíše zpátky"). Appka počítá POKUSY o generování (volání appky
    /tickets/generate), ne uložené tikety — appka platí za spuštění
    appčina modelu, ne za to, jestli si uživatel výsledek nechá.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            UPDATE users SET
                daily_generations_count = CASE
                    WHEN daily_generations_date = CURRENT_DATE THEN daily_generations_count + 1
                    ELSE 1
                END,
                daily_generations_date = CURRENT_DATE
            WHERE id = %s
            RETURNING daily_generations_count
            """,
            (user_id,),
        )
        return cur.fetchone()["daily_generations_count"]


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


def create_redeem_code(code: str, tokens: int, max_uses: int = 1, expires_at=None, note: str = "", unlimited_days: int = 0) -> None:
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO redeem_codes (code, tokens, max_uses, expires_at, note, unlimited_days) VALUES (%s, %s, %s, %s, %s, %s)",
            (code, tokens, max_uses, expires_at, note, unlimited_days or None),
        )


def redeem_code(code: str, user_id: int) -> dict:
    """
    Uplatní kód pro daného uživatele. Appka v jedné DB transakci: zamkne
    řádek kódu (FOR UPDATE, ať appka neuplatní stejný kód 2x souběžně nad
    limit), ověří platnost/limit/že ho tenhle uživatel ještě nepoužil,
    připíše tokeny (i 0, u čistě "unlimited" kódů) a zaloguje použití.
    Pokud má kód nastavené unlimited_days, appka navíc prodlouží/nastaví
    unlimited_until — od PODZDĚJŠÍHO z (teď, appka appky stávající
    unlimited_until), ať kód nikdy nezkrátí už běžící neomezený tarif.
    Vrací {"ok": True, "tokens": N, "balance": N, "unlimited_until": ISO|None}
    nebo {"ok": False, "error": "..."}.
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

        new_unlimited_until = None
        if row.get("unlimited_days"):
            cur.execute("SELECT unlimited_until FROM users WHERE id = %s FOR UPDATE", (user_id,))
            current_until = cur.fetchone()["unlimited_until"]
            now = datetime.now(timezone.utc)
            base = max(current_until, now) if current_until and current_until > now else now
            new_unlimited_until = base + timedelta(days=row["unlimited_days"])
            cur.execute("UPDATE users SET unlimited_until = %s WHERE id = %s", (new_unlimited_until, user_id))

        return {
            "ok": True, "tokens": row["tokens"], "balance": new_balance,
            "unlimited_until": new_unlimited_until.isoformat() if new_unlimited_until else None,
        }


def add_telegram_subscriber(chat_id: int, first_name: Optional[str]) -> bool:
    """Uloží/obnoví odběratele denních tiketů na Telegramu. Vrací True, pokud jde o NOVÉHO
    odběratele (appka mu má poslat uvítací zprávu), False, pokud tam chat_id už bylo."""
    with get_cursor() as cur:
        cur.execute("SELECT 1 FROM telegram_subscribers WHERE chat_id = %s", (chat_id,))
        is_new = cur.fetchone() is None
        cur.execute(
            """
            INSERT INTO telegram_subscribers (chat_id, first_name, active)
            VALUES (%s, %s, true)
            ON CONFLICT (chat_id) DO UPDATE SET active = true, first_name = EXCLUDED.first_name
            """,
            (chat_id, first_name),
        )
        return is_new


def get_active_telegram_subscribers() -> list[int]:
    with get_cursor() as cur:
        cur.execute("SELECT chat_id FROM telegram_subscribers WHERE active = true")
        return [row["chat_id"] for row in cur.fetchall()]


# =====================================================================
# Předplatné placeného Telegram kanálu
#
# Zdroj pravdy je Stripe. Appka si sem jen zrcadlí stav z webhooku a
# ptá se odsud při KAŽDÉ rozesílce — nikdy se nespoléhá na to, že si
# někoho jednou označila jako platícího a už to tak zůstane.
#
# Zákazník appce NEZAKLÁDÁ účet — platí přes samostatný Stripe Payment
# Link a appka ho pozná jen podle e-mailu a Stripe identifikátorů, které
# jí dá webhook. Klíčem je proto subscriptions.id (vlastní surogát), ne
# users.id.
# =====================================================================
ACTIVE_SUBSCRIPTION_STATUSES = ("active", "trialing")


def upsert_subscription_by_stripe_sub(
    stripe_subscription_id: str,
    email: str,
    status: str,
    stripe_customer_id: Optional[str] = None,
    current_period_end: Optional[datetime] = None,
) -> int:
    """
    Založí předplatné (nebo ho aktualizuje, pokud pro tenhle
    stripe_subscription_id appka řádek už má) a vrátí jeho id. COALESCE
    u volitelných sloupců appka používá schválně: některé webhook
    události (např. o zaplacení faktury) nenesou e-mail/customer ID, a
    appka jimi nesmí přepsat hodnoty, které si už dřív uložila z jiné
    události.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO subscriptions
                (email, stripe_customer_id, stripe_subscription_id, status, current_period_end, updated_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (stripe_subscription_id) DO UPDATE SET
                email                  = COALESCE(EXCLUDED.email, subscriptions.email),
                stripe_customer_id     = COALESCE(EXCLUDED.stripe_customer_id, subscriptions.stripe_customer_id),
                status                 = EXCLUDED.status,
                current_period_end     = COALESCE(EXCLUDED.current_period_end, subscriptions.current_period_end),
                updated_at             = now()
            RETURNING id
            """,
            (email, stripe_customer_id, stripe_subscription_id, status, current_period_end),
        )
        return cur.fetchone()["id"]


def update_subscription_by_stripe_id(
    stripe_subscription_id: str,
    status: str,
    current_period_end: Optional[datetime] = None,
) -> bool:
    """
    Aktualizuje předplatné podle Stripe ID — appka tohle potřebuje u
    událostí o obnovení/zrušení, které nenesou appce nic jiného než
    Stripe identifikátory. Vrací False, když k danému ID appka žádný
    řádek nemá (pak si volající musí založit nový přes
    upsert_subscription_by_stripe_sub, appka k tomu ale potřebuje email
    — ten se dotáhne ze Stripe Customer objektu).
    """
    with get_cursor() as cur:
        cur.execute(
            """
            UPDATE subscriptions
               SET status = %s,
                   current_period_end = COALESCE(%s, current_period_end),
                   updated_at = now()
             WHERE stripe_subscription_id = %s
            """,
            (status, current_period_end, stripe_subscription_id),
        )
        return cur.rowcount > 0


def get_subscription_by_id(subscription_id: int) -> Optional[dict]:
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, email, stripe_customer_id, stripe_subscription_id, status,
                   current_period_end, created_at, updated_at
              FROM subscriptions WHERE id = %s
            """,
            (subscription_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_subscription_by_email(email: str) -> Optional[dict]:
    """Nejnovější předplatné k danému e-mailu — appka to používá u
    veřejného odkazu na Stripe portál (bez přihlášení, jen podle
    e-mailu, který appka nikde jinde neověřuje)."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, email, stripe_customer_id, stripe_subscription_id, status, current_period_end
              FROM subscriptions WHERE email = %s
             ORDER BY created_at DESC LIMIT 1
            """,
            (email.strip().lower(),),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def has_active_subscription_id(subscription_id: int) -> bool:
    """
    Appka pouští placený obsah jen tomu, kdo má stav 'active' (nebo
    'trialing') A ZÁROVEŇ nevypršené období. Druhá podmínka je pojistka
    pro případ, že by appce utekl webhook o zrušení — předplatné pak
    samo dojede na konci zaplaceného období místo aby platilo věčně.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM subscriptions
             WHERE id = %s
               AND status = ANY(%s)
               AND (current_period_end IS NULL OR current_period_end > now())
            """,
            (subscription_id, list(ACTIVE_SUBSCRIPTION_STATUSES)),
        )
        return cur.fetchone() is not None


def get_paid_telegram_subscribers() -> list[dict]:
    """
    Chat_id všech, kdo mají PRÁVĚ TEĎ zaplaceno a zároveň spárovaný
    Telegram. Tímhle appka nahradila get_active_telegram_subscribers()
    v rozesílce — samotné /start bez platby sem člověka nedostane.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT ts.chat_id, ts.subscription_id, ts.first_name
              FROM telegram_subscribers ts
              JOIN subscriptions s ON s.id = ts.subscription_id
             WHERE ts.active = true
               AND ts.subscription_id IS NOT NULL
               AND s.status = ANY(%s)
               AND (s.current_period_end IS NULL OR s.current_period_end > now())
            """,
            (list(ACTIVE_SUBSCRIPTION_STATUSES),),
        )
        return [dict(r) for r in cur.fetchall()]


def get_lapsed_telegram_chats() -> list[dict]:
    """
    Spárované Telegramy, kterým předplatné doběhlo — appka jim pošle
    zprávu o vypršení a označí je jako neaktivní (viz
    /admin/telegram-sync). Bez tohohle kroku by jim sice tikety
    nechodily, ale nikdy by se nedozvěděli proč.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT ts.chat_id, ts.subscription_id
              FROM telegram_subscribers ts
              LEFT JOIN subscriptions s ON s.id = ts.subscription_id
             WHERE ts.active = true
               AND ts.subscription_id IS NOT NULL
               AND (
                    s.id IS NULL
                 OR s.status <> ALL(%s)
                 OR (s.current_period_end IS NOT NULL AND s.current_period_end <= now())
               )
            """,
            (list(ACTIVE_SUBSCRIPTION_STATUSES),),
        )
        return [dict(r) for r in cur.fetchall()]


def set_telegram_chat_active(chat_id: int, active: bool) -> None:
    with get_cursor() as cur:
        cur.execute("UPDATE telegram_subscribers SET active = %s WHERE chat_id = %s", (active, chat_id))


def create_telegram_link_code(code: str, subscription_id: int, ttl_minutes: int = 60) -> None:
    """Vydá jednorázový párovací kód. Starší nepoužité kódy TÉHOŽ
    předplatného appka zahodí, ať jich nezůstávají hromady platných."""
    with get_cursor() as cur:
        cur.execute("DELETE FROM telegram_link_codes WHERE subscription_id = %s AND used_at IS NULL", (subscription_id,))
        cur.execute(
            """
            INSERT INTO telegram_link_codes (code, subscription_id, expires_at)
            VALUES (%s, %s, now() + %s * interval '1 minute')
            """,
            (code, subscription_id, ttl_minutes),
        )


def consume_telegram_link_code(code: str) -> Optional[int]:
    """
    Uplatní párovací kód a vrátí subscription_id, kterému patří.
    Označení za použitý je součástí stejného UPDATE (ne zvlášť SELECT +
    UPDATE), aby dvě současná /start se stejným kódem nespárovala dva
    různé Telegramy.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            UPDATE telegram_link_codes
               SET used_at = now()
             WHERE code = %s AND used_at IS NULL AND expires_at > now()
            RETURNING subscription_id
            """,
            (code,),
        )
        row = cur.fetchone()
        return row["subscription_id"] if row else None


def link_telegram_chat(chat_id: int, subscription_id: int, first_name: Optional[str]) -> None:
    """
    Naváže chat_id na předplatné. Kdyby si tentýž zákazník spároval
    jiný Telegram, appka ten starý odpojí — jedno předplatné = jeden
    Telegram, jinak by stačilo koupit jedno a rozdat kód známým.
    """
    with get_cursor() as cur:
        cur.execute(
            "UPDATE telegram_subscribers SET subscription_id = NULL, active = false WHERE subscription_id = %s AND chat_id <> %s",
            (subscription_id, chat_id),
        )
        cur.execute(
            """
            INSERT INTO telegram_subscribers (chat_id, first_name, active, subscription_id)
            VALUES (%s, %s, true, %s)
            ON CONFLICT (chat_id) DO UPDATE
                SET active = true, first_name = EXCLUDED.first_name, subscription_id = EXCLUDED.subscription_id
            """,
            (chat_id, first_name, subscription_id),
        )


def get_subscription_id_for_chat(chat_id: int) -> Optional[int]:
    with get_cursor() as cur:
        cur.execute("SELECT subscription_id FROM telegram_subscribers WHERE chat_id = %s", (chat_id,))
        row = cur.fetchone()
        return row["subscription_id"] if row else None
