-- ============================================================
-- ApexSignal — databázové schéma
-- Cílová platforma: PostgreSQL 14+
-- ============================================================

CREATE TYPE risk_level_type        AS ENUM ('safe', 'aggressive');
CREATE TYPE ticket_status_type     AS ENUM ('pending', 'won', 'lost', 'void', 'cashed_out');
CREATE TYPE signal_type_enum       AS ENUM ('momentum', 'smart_correction');
CREATE TYPE signal_status_type     AS ENUM ('active', 'triggered', 'expired', 'dismissed');
CREATE TYPE correction_action_type AS ENUM ('cash_out', 'reduce_stake', 'hedge', 'hold');

-- ------------------------------------------------------------
-- 1. UŽIVATELÉ
-- ------------------------------------------------------------
CREATE TABLE users (
    id                  BIGSERIAL PRIMARY KEY,
    username            VARCHAR(50) UNIQUE NOT NULL,
    email               VARCHAR(255) UNIQUE NOT NULL,
    password_hash       VARCHAR(255) NOT NULL,
    subscription_tier   VARCHAR(20) NOT NULL DEFAULT 'free',
    default_risk_pct    SMALLINT NOT NULL DEFAULT 50 CHECK (default_risk_pct BETWEEN 0 AND 100),
    bankroll_amount     NUMERIC(12,2) DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at       TIMESTAMPTZ
);

-- ------------------------------------------------------------
-- 2. STATICKÉ LOOKUP TABULKY (sporty / trhy)
-- ------------------------------------------------------------
CREATE TABLE sports (
    id      SMALLSERIAL PRIMARY KEY,
    name    VARCHAR(30) UNIQUE NOT NULL          -- 'football' | 'tennis' | 'hockey' | 'basketball'
);

CREATE TABLE markets (
    id          SMALLSERIAL PRIMARY KEY,
    sport_id    SMALLINT NOT NULL REFERENCES sports(id),
    code        VARCHAR(50) NOT NULL,            -- 'match_winner' | 'over_2_5_goals' | 'over_4_5_cards'
    label       VARCHAR(100) NOT NULL,
    UNIQUE (sport_id, code)
);

-- ------------------------------------------------------------
-- 3. ZÁPASY
-- ------------------------------------------------------------
CREATE TABLE matches (
    id              BIGSERIAL PRIMARY KEY,
    external_id     VARCHAR(100),                -- ID z externího odds/stats API
    sport_id        SMALLINT NOT NULL REFERENCES sports(id),
    league          VARCHAR(100),
    home_team       VARCHAR(100) NOT NULL,
    away_team       VARCHAR(100) NOT NULL,
    start_time      TIMESTAMPTZ NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'scheduled',  -- scheduled | live | finished
    score_home      SMALLINT DEFAULT 0,
    score_away      SMALLINT DEFAULT 0,
    minute          SMALLINT DEFAULT 0,
    UNIQUE (external_id, sport_id)
);
CREATE INDEX idx_matches_status_start ON matches (status, start_time);

-- ------------------------------------------------------------
-- 4. LIVE STATISTIKY — časová řada pro Momentum Filter
-- ------------------------------------------------------------
CREATE TABLE match_live_stats (
    id                  BIGSERIAL PRIMARY KEY,
    match_id            BIGINT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    team_side           VARCHAR(4) NOT NULL CHECK (team_side IN ('home','away')),
    minute              SMALLINT NOT NULL,
    shots_on_target     SMALLINT DEFAULT 0,
    shots_total         SMALLINT DEFAULT 0,
    possession_pct      NUMERIC(5,2) DEFAULT 0,
    dangerous_attacks   SMALLINT DEFAULT 0,
    corners             SMALLINT DEFAULT 0,
    big_chances         SMALLINT DEFAULT 0,
    xg_cumulative       NUMERIC(5,3) DEFAULT 0,
    cards               SMALLINT DEFAULT 0,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_live_stats_match_minute ON match_live_stats (match_id, team_side, minute);

-- ------------------------------------------------------------
-- 5. TIKETY (Generátor tiketů)
-- ------------------------------------------------------------
CREATE TABLE tickets (
    id                      BIGSERIAL PRIMARY KEY,
    user_id                 BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    risk_type               risk_level_type NOT NULL,            -- safe (2–5) / aggressive (5–10)
    risk_slider_value       SMALLINT NOT NULL CHECK (risk_slider_value BETWEEN 0 AND 100),
    sports_filter           SMALLINT[] NOT NULL,                  -- zvolené sport_id
    timeframe_days          SMALLINT NOT NULL CHECK (timeframe_days BETWEEN 1 AND 5),
    total_odds              NUMERIC(6,2) NOT NULL,
    status                  ticket_status_type NOT NULL DEFAULT 'pending',
    is_saved                BOOLEAN NOT NULL DEFAULT false,
    generated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at               TIMESTAMPTZ
);
CREATE INDEX idx_tickets_user_status ON tickets (user_id, status);

-- ------------------------------------------------------------
-- 6. JEDNOTLIVÉ TIPY V TIKETU (kombinace trhů)
-- ------------------------------------------------------------
CREATE TABLE ticket_legs (
    id                      BIGSERIAL PRIMARY KEY,
    ticket_id               BIGINT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    match_id                BIGINT NOT NULL REFERENCES matches(id),
    market_id               SMALLINT NOT NULL REFERENCES markets(id),
    selection               VARCHAR(100) NOT NULL,                -- 'home_win' | 'over_2.5' | ...
    odds                    NUMERIC(6,2) NOT NULL,
    model_probability_pct   NUMERIC(5,2) NOT NULL CHECK (model_probability_pct >= 70),  -- striktní podmínka
    leg_order               SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_legs_ticket ON ticket_legs (ticket_id);

-- ------------------------------------------------------------
-- 7. HISTORIE / VYHODNOCENÍ TIKETŮ
-- ------------------------------------------------------------
CREATE TABLE ticket_history (
    id              BIGSERIAL PRIMARY KEY,
    ticket_id       BIGINT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    result          ticket_status_type NOT NULL,
    stake_amount    NUMERIC(12,2),
    profit_loss     NUMERIC(12,2),
    note            TEXT,
    settled_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- 8. LIVE SIGNÁLY (Live Signal Engine — output)
-- ------------------------------------------------------------
CREATE TABLE live_signals (
    id                      BIGSERIAL PRIMARY KEY,
    match_id                BIGINT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    market_id               SMALLINT REFERENCES markets(id),
    signal_type              signal_type_enum NOT NULL,
    team_side                VARCHAR(4) CHECK (team_side IN ('home','away')),
    recommended_selection    VARCHAR(100),
    current_odds             NUMERIC(6,2),
    momentum_score           NUMERIC(5,1),
    reasoning_text           TEXT NOT NULL,
    recommended_stake_pct    NUMERIC(4,1),
    status                   signal_status_type NOT NULL DEFAULT 'active',
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at                TIMESTAMPTZ
);
CREATE INDEX idx_signals_match_status ON live_signals (match_id, status);

-- ------------------------------------------------------------
-- 9. SMART-CORRECTION UDÁLOSTI (návazné na live_signals)
-- ------------------------------------------------------------
CREATE TABLE smart_corrections (
    id                   BIGSERIAL PRIMARY KEY,
    original_signal_id   BIGINT REFERENCES live_signals(id) ON DELETE CASCADE,
    match_id              BIGINT NOT NULL REFERENCES matches(id),
    trigger_event          VARCHAR(50) NOT NULL,         -- 'red_card' | 'tempo_shift' | 'goal_against'
    recommended_action     correction_action_type NOT NULL,
    explanation             TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- 10. IN-APP NOTIFIKACE
-- ------------------------------------------------------------
CREATE TABLE notifications (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    signal_id       BIGINT REFERENCES live_signals(id),
    correction_id   BIGINT REFERENCES smart_corrections(id),
    title           VARCHAR(150) NOT NULL,
    body            TEXT NOT NULL,
    is_read         BOOLEAN NOT NULL DEFAULT false,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
