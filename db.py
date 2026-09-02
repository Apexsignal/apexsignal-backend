"""
db.py — PostgreSQL perzistence pro ApexSignal.
"""
from __future__ import annotations

import json
import os
import secrets
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

-- Stejný vzor jako password_reset_tokens — appka tímhle ověřuje, že
-- e-mail zadaný při registraci reálně existuje a uživatel na něj má
-- přístup, PŘED tím, než mu připíše zkušební tokeny zdarma. Bez tohohle
-- šlo dokola zakládat nové účty s vymyšlenými e-maily jen kvůli
-- opakovanému zkušebnímu tiketu.
CREATE TABLE IF NOT EXISTS email_verification_tokens (
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

-- Trvalé přepínače/nastavení appky (na rozdíl od api_cache výše, tady
-- appka NEMÁ TTL — hodnota platí, dokud ji appka výslovně nepřepíše).
-- První použití: DIXON_COLES_ENABLED (viz backend_api.py) — appka to
-- chtěla umět zapnout/vypnout tlačítkem v appce bez ručního zásahu na
-- Renderu a bez redeploye.
CREATE TABLE IF NOT EXISTS app_settings (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT now()
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

    # Neomezené generování (9900 Kč/měsíc, viz _require_generation_enabled
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

    # Zkušební kódy appka umí omezit na nižší denní strop, než má placený
    # neomezený tarif (UNLIMITED_GENERATION_DAILY_CAP) — např. "3 dny
    # zdarma, max 5 generování/den". NULL = appka použije standardní
    # strop. set_unlimited_until appka volá jen z PLACENÝCH/administrátorem
    # schválených cest (Stripe webhook, /admin/set-unlimited), takže appka
    # tam override vždy vynuluje — jinak by kdysi uplatněný zkušební kód
    # navždy omezoval i pozdějšího platícího zákazníka.
    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_generation_cap_override INTEGER")
    except Exception:
        pass
    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE redeem_codes ADD COLUMN IF NOT EXISTS daily_cap_override INTEGER")
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

    # Bez tohohle appka nedokáže rozlišit "e-mail reálně existuje a
    # uživatel na něj klikl" od "kdokoliv zadal cokoliv při registraci" —
    # klíčové proti opakovanému zakládání účtů jen kvůli zkušebním tokenům.
    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT false")
    except Exception:
        pass

    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_generations_date DATE")
    except Exception:
        pass

    # Doporučovací systém — kód appka generuje líně (až při první potřebě,
    # ne při registraci), referred_by appka nastaví JEDNOU při registraci
    # a napořád (viz set_referred_by), aby ho pozdější ?ref= odkaz nemohl
    # přepsat.
    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code VARCHAR(16) UNIQUE")
    except Exception:
        pass

    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by_user_id INTEGER REFERENCES users(id)")
    except Exception:
        pass

    # UNIQUE na referred_user_id = appka nemůže odměnit stejný doporučený
    # účet dvakrát, ani omylem (viz _process_referral_reward).
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS referral_rewards (
                    id SERIAL PRIMARY KEY,
                    referred_user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    referrer_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    referred_tokens INTEGER NOT NULL,
                    referrer_tokens INTEGER NOT NULL,
                    card_fingerprint VARCHAR(64),
                    created_at TIMESTAMPTZ DEFAULT now()
                )
                """
            )
    except Exception:
        pass

    # Appka si sem loguje otisk platební karty (ne číslo karty) u KAŽDÉHO
    # nákupu tokenů — díky tomu appka umí u doporučovacího systému poznat,
    # že doporučený a doporučitel platí stejnou kartou, i kdyby měli různé
    # e-maily/účty (viz _process_referral_reward).
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_card_fingerprints (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    fingerprint VARCHAR(64) NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (user_id, fingerprint)
                )
                """
            )
    except Exception:
        pass

    # Appka tímhle sleduje, co registrovaní uživatelé reálně dělají — klik
    # na Vygenerovat, úspěšné/neúspěšné generování, uložení tiketu, a
    # pravidelný heartbeat (dá odhad času stráveného na webu — appka ho
    # posílá z frontendu, dokud je karta aktivní/viditelná).
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_events (
                    id BIGSERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    event_type VARCHAR(40) NOT NULL,
                    session_id VARCHAR(64),
                    metadata JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
    except Exception:
        pass
    try:
        with get_cursor() as cur:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_user_events_user_id ON user_events (user_id)")
    except Exception:
        pass
    try:
        with get_cursor() as cur:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_user_events_session ON user_events (session_id)")
    except Exception:
        pass

    # Prodejci (Pepa a další) appce přivádí platící klienty přes appčiny
    # pevné Stripe Payment Linky (500/1000/2000/2500/3000 Kč) — seller_code
    # jde do Stripe jako client_reference_id, appka podle něj ve webhooku
    # pozná, čí klient zaplatil (viz _seller_commission_split).
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sellers (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    seller_code VARCHAR(32) UNIQUE NOT NULL,
                    display_name VARCHAR(120) NOT NULL,
                    telegram_chat_id BIGINT,
                    active BOOLEAN NOT NULL DEFAULT true,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
    except Exception:
        pass
    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE sellers ADD COLUMN IF NOT EXISTS telegram_chat_id BIGINT")
    except Exception:
        pass

    # Appka sem loguje KAŽDOU platbu přivedeného klienta — jedna řádka =
    # jedna platba, ne souhrn. Díky tomu appka umí ukázat prodejci celou
    # historii, ne jen aktuální stav, a stripe_checkout_session_id appce
    # zabrání připsat stejnou platbu dvakrát (viz webhook).
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS seller_earnings (
                    id SERIAL PRIMARY KEY,
                    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                    stripe_checkout_session_id VARCHAR(120) UNIQUE NOT NULL,
                    client_email VARCHAR(255),
                    tier_price_kc INTEGER NOT NULL,
                    our_cut_kc INTEGER NOT NULL,
                    seller_cut_kc INTEGER NOT NULL,
                    paid_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
    except Exception:
        pass
    try:
        with get_cursor() as cur:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_seller_earnings_seller_id ON seller_earnings (seller_id)")
    except Exception:
        pass
    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE seller_earnings ADD COLUMN IF NOT EXISTS stripe_invoice_id VARCHAR(120) UNIQUE")
    except Exception:
        pass
    # Appka dřív provizi zapisovala JEN při první platbě (checkout.session.
    # completed) — každé další automatické obnovení (týden/2 týdny/měsíc)
    # appka appce nezapočítala vůbec, protože obnovení appce chodí přes
    # invoice.payment_succeeded, ne přes novou checkout session. Tahle
    # tabulka appce drží ŽIVÝ stav předplatného ke KAŽDÉMU odkazu na
    # Stripe subscription (kdy končí, jestli je pořád aktivní) — appka to
    # potřebuje, aby uměla webhooku na invoice.payment_succeeded/
    # customer.subscription.* říct, čí prodejcovo předplatné se právě
    # obnovilo nebo zrušilo (nahlásil uživatel 2026-08-26).
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS seller_client_subscriptions (
                    stripe_subscription_id VARCHAR(120) PRIMARY KEY,
                    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                    client_email VARCHAR(255),
                    tier_price_kc INTEGER NOT NULL,
                    our_cut_kc INTEGER NOT NULL,
                    seller_cut_kc INTEGER NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    current_period_end TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
    except Exception:
        pass
    try:
        with get_cursor() as cur:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_seller_client_subs_seller_id ON seller_client_subscriptions (seller_id)")
    except Exception:
        pass

    # Přihlášky z veřejného náborového formuláře (/prihlaska) — appka
    # tudy sbírá zájemce PŘED tím, než si vůbec založí účet appky, proto
    # je to samostatná tabulka bez vazby na users/sellers.
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS seller_leads (
                    id SERIAL PRIMARY KEY,
                    full_name VARCHAR(160) NOT NULL,
                    age INTEGER,
                    city VARCHAR(120),
                    experience TEXT,
                    start_when VARCHAR(60),
                    income_goal VARCHAR(60),
                    can_work_online BOOLEAN,
                    contact VARCHAR(255) NOT NULL,
                    phone VARCHAR(40),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
    except Exception:
        pass
    try:
        with get_cursor() as cur:
            cur.execute("ALTER TABLE seller_leads ADD COLUMN IF NOT EXISTS phone VARCHAR(40)")
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


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Trvalý přepínač appky (viz app_settings výše) — na rozdíl od
    cache_get/cache_set tady appka nemá TTL, hodnota platí, dokud ji
    appka výslovně nepřepíše přes set_setting."""
    with get_cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
        row = cur.fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_cursor() as cur:
        cur.execute(
            """INSERT INTO app_settings (key, value, updated_at)
               VALUES (%s, %s, now())
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
            (key, value),
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


def get_recent_registrations(days: int = 1) -> list[dict]:
    """Appka tímhle appce vypíše e-maily a čas registrace nedávno
    založených účtů — doplněk ke get_conversion_funnel, který appce dá
    jen počet, ne kdo konkrétně."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT email, created_at FROM users WHERE created_at > now() - %s * interval '1 day' ORDER BY created_at DESC",
            (days,),
        )
        return [{"email": row["email"], "created_at": row["created_at"]} for row in cur.fetchall()]


def log_user_event(user_id: int, event_type: str, session_id: Optional[str] = None, metadata: Optional[dict] = None) -> None:
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO user_events (user_id, event_type, session_id, metadata) VALUES (%s, %s, %s, %s)",
            (user_id, event_type, session_id, json.dumps(metadata) if metadata is not None else None),
        )


def get_user_activity_summary(days: int = 1) -> list[dict]:
    """
    Appka appce ukáže, co nedávno registrovaní uživatelé reálně dělají:
    kolikrát klikli na Vygenerovat, kolikrát appka reálně vygenerovala,
    kolik tiketů si uložili (appka to bere z tabulky tickets, ne z
    eventů — je to zdroj pravdy, event by mohl chybět, kdyby appka
    frontend někdy zapomněla zalogovat), a odhad času na webu appka
    spočítá ze session_id heartbeatů (poslední mínus první v rámci
    jedné session, sečteno přes všechny appky session appky uživatele).
    """
    with get_cursor() as cur:
        cur.execute(
            """
            WITH recent_users AS (
                SELECT id, email, created_at FROM users WHERE created_at > now() - %s * interval '1 day'
            ),
            session_spans AS (
                SELECT user_id, session_id, MIN(created_at) AS started, MAX(created_at) AS ended
                FROM user_events
                WHERE session_id IS NOT NULL
                GROUP BY user_id, session_id
            ),
            time_per_user AS (
                SELECT user_id, SUM(EXTRACT(EPOCH FROM (ended - started))) AS seconds
                FROM session_spans
                GROUP BY user_id
            ),
            clicks AS (
                SELECT user_id,
                    COUNT(*) FILTER (WHERE event_type = 'click_generate') AS clicked_generate,
                    COUNT(*) FILTER (WHERE event_type = 'generate_success') AS generated,
                    COUNT(*) FILTER (WHERE event_type = 'generate_failed') AS generate_failed
                FROM user_events
                GROUP BY user_id
            ),
            saved AS (
                SELECT user_id, COUNT(*) AS n FROM tickets GROUP BY user_id
            )
            SELECT
                ru.id AS user_id, ru.email, ru.created_at,
                COALESCE(c.clicked_generate, 0) AS clicked_generate,
                COALESCE(c.generated, 0) AS generated,
                COALESCE(c.generate_failed, 0) AS generate_failed,
                COALESCE(s.n, 0) AS saved,
                COALESCE(tp.seconds, 0) AS seconds_on_site
            FROM recent_users ru
            LEFT JOIN clicks c ON c.user_id = ru.id
            LEFT JOIN saved s ON s.user_id = ru.id
            LEFT JOIN time_per_user tp ON tp.user_id = ru.id
            ORDER BY ru.created_at DESC
            """,
            (days,),
        )
        return cur.fetchall()


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


def create_email_verification_token(token: str, user_id: int, expires_at) -> None:
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO email_verification_tokens (token, user_id, expires_at) VALUES (%s, %s, %s)",
            (token, user_id, expires_at),
        )


def consume_email_verification_token(token: str) -> Optional[int]:
    """Stejná logika jako consume_password_reset_token — appka token
    ověří a rovnou označí jako použitý v jedné transakci se zamčením
    řádku, ať nejde stejný odkaz uplatnit dvakrát souběžně."""
    with get_cursor() as cur:
        cur.execute("SELECT * FROM email_verification_tokens WHERE token = %s FOR UPDATE", (token,))
        row = cur.fetchone()
        if row is None or row["used"] or row["expires_at"] < datetime.now():
            return None
        cur.execute("UPDATE email_verification_tokens SET used = true WHERE token = %s", (token,))
        return row["user_id"]


def set_email_verified(user_id: int) -> None:
    with get_cursor() as cur:
        cur.execute("UPDATE users SET email_verified = true WHERE id = %s", (user_id,))


def is_email_verified(user_id: int) -> bool:
    with get_cursor() as cur:
        cur.execute("SELECT email_verified FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return bool(row and row["email_verified"])


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
        "user_id": ticket_row["user_id"],
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


def count_token_transactions_with_reason(user_id: int, reason: str) -> int:
    with get_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM token_transactions WHERE user_id = %s AND reason = %s",
            (user_id, reason),
        )
        return cur.fetchone()["c"]


# =====================================================================
# Doporučovací systém (viz backend_api.py: _process_referral_reward)
# =====================================================================
_REFERRAL_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # bez 0/O a 1/I, ať se to nepřehazuje při přepisu


def get_or_create_referral_code(user_id: int) -> str:
    """
    Appka kód generuje líně (až při první potřebě), ne rovnou při
    registraci — drtivá většina účtů si o něj nikdy nepožádá. Kolizi
    appka řeší zkusit-znovu, ne appka to nikdy needituje ručně.
    """
    with get_cursor() as cur:
        cur.execute("SELECT referral_code FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if row and row["referral_code"]:
            return row["referral_code"]

    for _ in range(10):
        code = "".join(secrets.choice(_REFERRAL_CODE_ALPHABET) for _ in range(6))
        try:
            with get_cursor() as cur:
                cur.execute(
                    "UPDATE users SET referral_code = %s WHERE id = %s AND referral_code IS NULL RETURNING referral_code",
                    (code, user_id),
                )
                row = cur.fetchone()
                if row:
                    return row["referral_code"]
        except Exception:
            continue  # kolize kódu (UNIQUE), appka zkusí jiný náhodný kód
    raise RuntimeError("Appce se nepodařilo vygenerovat unikátní referral kód")


def get_user_id_by_referral_code(code: str) -> Optional[int]:
    with get_cursor() as cur:
        cur.execute("SELECT id FROM users WHERE referral_code = %s", (code.strip().upper(),))
        row = cur.fetchone()
        return row["id"] if row else None


def set_referred_by(user_id: int, referrer_user_id: int) -> None:
    """
    Appka tohle volá jen PŘI REGISTRACI nového účtu — podmínka
    `referred_by_user_id IS NULL` je tu jako pojistka, appka referred_by
    nikdy nepřepisuje podruhé (i kdyby se stejná registrace omylem
    zavolala dvakrát).
    """
    if referrer_user_id == user_id:
        return
    with get_cursor() as cur:
        cur.execute(
            "UPDATE users SET referred_by_user_id = %s WHERE id = %s AND referred_by_user_id IS NULL",
            (referrer_user_id, user_id),
        )


def get_referred_by(user_id: int) -> Optional[int]:
    with get_cursor() as cur:
        cur.execute("SELECT referred_by_user_id FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return row["referred_by_user_id"] if row else None


def has_referral_reward(referred_user_id: int) -> bool:
    with get_cursor() as cur:
        cur.execute("SELECT 1 FROM referral_rewards WHERE referred_user_id = %s", (referred_user_id,))
        return cur.fetchone() is not None


def count_referral_rewards_for_referrer_since(referrer_user_id: int, since: datetime) -> int:
    with get_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM referral_rewards WHERE referrer_user_id = %s AND created_at >= %s",
            (referrer_user_id, since),
        )
        return cur.fetchone()["c"]


def create_referral_reward(
    referred_user_id: int, referrer_user_id: int, referred_tokens: int, referrer_tokens: int,
    card_fingerprint: Optional[str],
) -> None:
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO referral_rewards
                (referred_user_id, referrer_user_id, referred_tokens, referrer_tokens, card_fingerprint)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (referred_user_id) DO NOTHING
            """,
            (referred_user_id, referrer_user_id, referred_tokens, referrer_tokens, card_fingerprint),
        )


def record_card_fingerprint(user_id: int, fingerprint: Optional[str]) -> None:
    """Appka appce jen loguje otisky karet, co appka kdy viděla u nákupu
    tokenů — appka to nepoužívá k ničemu jinému než k detekci sdílené
    karty mezi doporučeným a doporučitelem."""
    if not fingerprint:
        return
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_card_fingerprints (user_id, fingerprint)
            VALUES (%s, %s)
            ON CONFLICT (user_id, fingerprint) DO NOTHING
            """,
            (user_id, fingerprint),
        )


def get_card_fingerprints(user_id: int) -> set[str]:
    with get_cursor() as cur:
        cur.execute("SELECT fingerprint FROM user_card_fingerprints WHERE user_id = %s", (user_id,))
        return {r["fingerprint"] for r in cur.fetchall()}


# =====================================================================
# Prodejci (provizní systém) — viz sellers/seller_earnings v ensure_schema.
# =====================================================================
def create_seller(user_id: int, seller_code: str, display_name: str, active: bool = True) -> int:
    """`active` slouží jako schvalovací příznak — self-serve /seller/apply
    zakládá nového prodejce s active=False (čeká na ruční schválení),
    admin /admin/sellers/create ho zakládá rovnou jako active=True.
    ON CONFLICT sloupec active nikdy nemění, aby opakovaná registrace
    už schváleného prodejce nezresetovala jeho stav zpátky na čekající."""
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO sellers (user_id, seller_code, display_name, active)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET seller_code = EXCLUDED.seller_code, display_name = EXCLUDED.display_name
            RETURNING id
            """,
            (user_id, seller_code, display_name, active),
        )
        return cur.fetchone()["id"]


def get_seller_by_user_id(user_id: int) -> Optional[dict]:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM sellers WHERE user_id = %s AND active = true", (user_id,))
        return cur.fetchone()


def get_seller_by_user_id_any(user_id: int) -> Optional[dict]:
    """Jako get_seller_by_user_id, ale i neschválené (active=false) —
    /seller/apply a /seller/dashboard tohle potřebují, aby uměly
    rozlišit "neregistrovaný" od "čeká na schválení"."""
    with get_cursor() as cur:
        cur.execute("SELECT * FROM sellers WHERE user_id = %s", (user_id,))
        return cur.fetchone()


def list_pending_sellers() -> list[dict]:
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.seller_code, s.display_name, s.created_at, u.email
              FROM sellers s
              JOIN users u ON u.id = s.user_id
             WHERE s.active = false
             ORDER BY s.created_at ASC
            """
        )
        return cur.fetchall()


def get_seller_by_code_any(seller_code: str) -> Optional[dict]:
    """Jako get_seller_by_code, ale i neschválené (active=false) a s
    appčiným e-mailem rovnou přibaleným — appka to potřebuje po schválení,
    aby appce věděla, kam poslat potvrzovací e-mail."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT s.*, u.email AS user_email
              FROM sellers s
              JOIN users u ON u.id = s.user_id
             WHERE s.seller_code = %s
            """,
            (seller_code,),
        )
        return cur.fetchone()


def set_seller_active(seller_code: str, active: bool) -> bool:
    with get_cursor() as cur:
        cur.execute(
            "UPDATE sellers SET active = %s WHERE seller_code = %s RETURNING id",
            (active, seller_code),
        )
        return cur.fetchone() is not None


def create_seller_lead(
    full_name: str,
    age: Optional[int],
    city: Optional[str],
    experience: Optional[str],
    start_when: Optional[str],
    income_goal: Optional[str],
    can_work_online: Optional[bool],
    contact: str,
    phone: Optional[str] = None,
) -> int:
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO seller_leads (full_name, age, city, experience, start_when, income_goal, can_work_online, contact, phone)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (full_name, age, city, experience, start_when, income_goal, can_work_online, contact, phone),
        )
        return cur.fetchone()["id"]


def list_seller_leads() -> list[dict]:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM seller_leads ORDER BY created_at DESC")
        return cur.fetchall()


def get_seller_by_code(seller_code: str) -> Optional[dict]:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM sellers WHERE seller_code = %s AND active = true", (seller_code,))
        return cur.fetchone()


def link_seller_telegram(seller_code: str, chat_id: int) -> bool:
    """Appka appce spáruje prodejcovo Telegram chat_id podle jeho
    seller_code (z odkazu https://t.me/BOT?start=seller_<code>) — appka
    díky tomu umí prodejci poslat DM při každé nové platbě."""
    with get_cursor() as cur:
        cur.execute(
            "UPDATE sellers SET telegram_chat_id = %s WHERE seller_code = %s AND active = true",
            (chat_id, seller_code),
        )
        return cur.rowcount > 0


def record_seller_earning(
    seller_id: int, stripe_checkout_session_id: str, client_email: Optional[str],
    tier_price_kc: int, our_cut_kc: int, seller_cut_kc: int,
) -> bool:
    """Appka appce vrátí True, jen když reálně zapsala NOVOU platbu —
    stripe_checkout_session_id je UNIQUE, takže appka stejnou platbu
    (např. při zopakovaném Stripe webhooku) nikdy nepřipíše dvakrát."""
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO seller_earnings
                (seller_id, stripe_checkout_session_id, client_email, tier_price_kc, our_cut_kc, seller_cut_kc)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (stripe_checkout_session_id) DO NOTHING
            """,
            (seller_id, stripe_checkout_session_id, client_email, tier_price_kc, our_cut_kc, seller_cut_kc),
        )
        return cur.rowcount > 0


def record_seller_renewal_earning(
    seller_id: int, stripe_invoice_id: str, client_email: Optional[str],
    tier_price_kc: int, our_cut_kc: int, seller_cut_kc: int,
) -> bool:
    """Appka appce zapíše provizi za KAŽDÉ automatické obnovení (invoice.
    payment_succeeded, appka to appce vyžádala 2026-08-26) — appka na to
    použije stejnou tabulku jako u první platby, jen appka idempotenci
    hlídá přes stripe_invoice_id (checkout_session_id appka nemá — appka
    obnovení nikdy neprochází přes novou checkout session)."""
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO seller_earnings
                (seller_id, stripe_checkout_session_id, stripe_invoice_id, client_email, tier_price_kc, our_cut_kc, seller_cut_kc)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (stripe_invoice_id) DO NOTHING
            """,
            (seller_id, f"invoice_{stripe_invoice_id}", stripe_invoice_id, client_email, tier_price_kc, our_cut_kc, seller_cut_kc),
        )
        return cur.rowcount > 0


def upsert_seller_subscription(
    stripe_subscription_id: str, seller_id: int, client_email: Optional[str],
    tier_price_kc: int, our_cut_kc: int, seller_cut_kc: int,
    status: str, current_period_end: Optional[datetime],
) -> None:
    """Appka appce drží ŽIVÝ stav prodejcova klientova předplatného —
    appka ho zapíše/aktualizuje při KAŽDÉ relevantní Stripe události
    (nová platba, obnovení, zrušení), ať appka umí kdykoli odpovědět
    'kdy tomuhle klientovi končí předplatné' bez dotazu na Stripe."""
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO seller_client_subscriptions
                (stripe_subscription_id, seller_id, client_email, tier_price_kc, our_cut_kc, seller_cut_kc, status, current_period_end, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (stripe_subscription_id) DO UPDATE SET
                status = EXCLUDED.status,
                current_period_end = COALESCE(EXCLUDED.current_period_end, seller_client_subscriptions.current_period_end),
                client_email = COALESCE(EXCLUDED.client_email, seller_client_subscriptions.client_email),
                updated_at = now()
            """,
            (stripe_subscription_id, seller_id, client_email, tier_price_kc, our_cut_kc, seller_cut_kc, status, current_period_end),
        )


def get_seller_subscription(stripe_subscription_id: str) -> Optional[dict]:
    """Appka sem sahá ve webhooku appce zjistit, jestli daná Stripe
    subscription patří appce NĚJAKÉHO prodejce — appka to appce použije
    dřív, než appka pro appku vůbec zkusí sáhnout do appčiných branchí
    pro appčin vlastní kanál/neomezené generování."""
    with get_cursor() as cur:
        cur.execute("SELECT * FROM seller_client_subscriptions WHERE stripe_subscription_id = %s", (stripe_subscription_id,))
        return cur.fetchone()


def list_seller_subscriptions(seller_id: int) -> list[dict]:
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM seller_client_subscriptions WHERE seller_id = %s ORDER BY current_period_end ASC NULLS LAST",
            (seller_id,),
        )
        return cur.fetchall()


def list_all_seller_client_subscriptions() -> list[dict]:
    """Appka appce (adminovi) ukáže klienty VŠECH prodejců najednou,
    seřazené podle toho, komu appka nejdřív skončí předplatné — appka to
    potřebuje na /admin-prodejci, ať appka vidí blížící se konce bez
    nutnosti proklikávat každého prodejce zvlášť."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT sub.*, s.seller_code, s.display_name AS seller_name
              FROM seller_client_subscriptions sub
              JOIN sellers s ON s.id = sub.seller_id
             ORDER BY sub.current_period_end ASC NULLS LAST
            """
        )
        return cur.fetchall()


def list_channel_subscriptions() -> list[dict]:
    """Appka appce (adminovi) ukáže VŠECHNY přímé předplatitele appčina
    Telegram kanálu (990 Kč/měsíc, tabulka subscriptions) seřazené podle
    toho, komu appka nejdřív skončí předplatné — obdoba
    list_all_seller_client_subscriptions, jen appka pro tenhle přímý
    kanál dřív žádný takový přehled neměla vůbec."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT email, status, current_period_end, created_at "
            "FROM subscriptions ORDER BY current_period_end ASC NULLS LAST"
        )
        return cur.fetchall()


def get_active_sellers_with_telegram() -> list[dict]:
    """Appka sem sahá při denní rozesílce (viz /admin/client-tickets-send)
    — appka posílá appčin denní tiket i prodejcům, ne jen platícím
    odběratelům appčina kanálu, ať mají co přeposílat do svého kanálu."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT id, seller_code, telegram_chat_id FROM sellers WHERE active = true AND telegram_chat_id IS NOT NULL"
        )
        return cur.fetchall()


def get_seller_earnings(seller_id: int) -> list[dict]:
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM seller_earnings WHERE seller_id = %s ORDER BY paid_at DESC",
            (seller_id,),
        )
        return cur.fetchall()


def list_sellers_overview() -> list[dict]:
    """Appka appce vrátí VŠECHNY aktivní prodejce najednou se souhrnnou
    provizí — appka na to dřív neměla nic, jen dashboard jednotlivého
    prodejce (/seller/dashboard), který appce (uživateli) nic neukáže
    napříč všemi najednou (viz /admin/sellers/overview)."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.seller_code, s.display_name, s.telegram_chat_id, s.created_at, u.email,
                   COALESCE(subs.total_clients, 0) AS total_clients,
                   COALESCE(subs.active_clients, 0) AS active_clients,
                   COALESCE(earn.total_seller_kc, 0) AS total_seller_kc,
                   COALESCE(earn.total_our_kc, 0) AS total_our_kc,
                   earn.last_paid_at AS last_paid_at
              FROM sellers s
              JOIN users u ON u.id = s.user_id
              LEFT JOIN (
                    SELECT seller_id, COUNT(*) AS total_clients,
                           COUNT(*) FILTER (WHERE status IN ('active', 'trialing')) AS active_clients
                      FROM seller_client_subscriptions
                     GROUP BY seller_id
                   ) subs ON subs.seller_id = s.id
              LEFT JOIN (
                    SELECT seller_id, SUM(seller_cut_kc) AS total_seller_kc,
                           SUM(our_cut_kc) AS total_our_kc, MAX(paid_at) AS last_paid_at
                      FROM seller_earnings
                     GROUP BY seller_id
                   ) earn ON earn.seller_id = s.id
             WHERE s.active = true
             ORDER BY total_seller_kc DESC NULLS LAST, s.created_at ASC
            """
        )
        return cur.fetchall()


# =====================================================================
# Neomezené generování (9900 Kč/měsíc, strop 10 generování/den)
# =====================================================================
def get_unlimited_until(user_id: int) -> Optional[datetime]:
    with get_cursor() as cur:
        cur.execute("SELECT unlimited_until FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return row["unlimited_until"] if row else None


def set_unlimited_until(user_id: int, until: datetime, stripe_customer_id: Optional[str] = None) -> None:
    """Appka tudy pouští jen placené (Stripe webhook) nebo administrátorem
    schválené (/admin/set-unlimited, /admin/provision-account) aktivace —
    proto appka při každém volání zároveň vynuluje daily_generation_cap_override:
    jinak by dřív uplatněný zkušební kód s nižším stropem navždy omezoval
    i zákazníka, co si pak koupí plný tarif. Nižší strop appka nastavuje
    JEN uvnitř redeem_code, pro konkrétní zkušební kódy."""
    with get_cursor() as cur:
        if stripe_customer_id is not None:
            cur.execute(
                "UPDATE users SET unlimited_until = %s, unlimited_stripe_customer_id = %s, daily_generation_cap_override = NULL WHERE id = %s",
                (until, stripe_customer_id, user_id),
            )
        else:
            cur.execute("UPDATE users SET unlimited_until = %s, daily_generation_cap_override = NULL WHERE id = %s", (until, user_id))


def get_daily_generation_cap_override(user_id: int) -> Optional[int]:
    with get_cursor() as cur:
        cur.execute("SELECT daily_generation_cap_override FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return row["daily_generation_cap_override"] if row else None


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


def create_redeem_code(
    code: str, tokens: int, max_uses: int = 1, expires_at=None, note: str = "",
    unlimited_days: int = 0, daily_cap_override: int = 0,
) -> None:
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO redeem_codes (code, tokens, max_uses, expires_at, note, unlimited_days, daily_cap_override) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (code, tokens, max_uses, expires_at, note, unlimited_days or None, daily_cap_override or None),
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
            if row.get("daily_cap_override"):
                cur.execute(
                    "UPDATE users SET unlimited_until = %s, daily_generation_cap_override = %s WHERE id = %s",
                    (new_unlimited_until, row["daily_cap_override"], user_id),
                )
            else:
                cur.execute("UPDATE users SET unlimited_until = %s WHERE id = %s", (new_unlimited_until, user_id))

        return {
            "ok": True, "tokens": row["tokens"], "balance": new_balance,
            "unlimited_until": new_unlimited_until.isoformat() if new_unlimited_until else None,
            "daily_cap_override": row.get("daily_cap_override"),
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
