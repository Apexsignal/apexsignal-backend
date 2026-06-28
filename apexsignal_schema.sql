-- =====================================================================
-- ApexSignal — databázové schéma (PostgreSQL dialekt)
-- Moduly: Generátor tiketů + Live Signal Engine
--
-- POZN. K AKTUÁLNÍMU STAVU (appka toto schéma reálně používá z db.py):
-- - tickets + ticket_selections: AKTIVNĚ používané, appka do nich
--   ukládá a z nich čte (historie, track record, kalibrace, ROI).
-- - users, matches, match_stats_timeline, live_signals, bet_history:
--   appka tyto tabulky NEPOPULUJE — nemá žádný auth systém (user_id je
--   v kódu napevno 1) a live signály appka zatím záměrně nechává jen
--   v paměti běžícího procesu (viz komentář u Repo v backend_api.py).
--   Tabulky tu zůstávají jako návrh pro budoucí rozšíření, ne jako
--   popis dnešní reality.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. UŽIVATELÉ
-- ---------------------------------------------------------------------
CREATE TABLE users (
    id                  BIGSERIAL PRIMARY KEY,
    email               VARCHAR(255) UNIQUE NOT NULL,
    password_hash       VARCHAR(255) NOT NULL,
    subscription_tier   VARCHAR(20) NOT NULL DEFAULT 'free',   -- free / pro / vip
    default_risk_level  SMALLINT DEFAULT 50,                   -- posuvník 0-100 %
    preferred_sports    TEXT[],                                 -- {'football','tennis',...}
    bankroll_amount     NUMERIC(12,2) DEFAULT 0,                -- pro výpočet % vkladu
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now()
);

-- ---------------------------------------------------------------------
-- 2. ZÁPASY (sdílené mezi tiketovým generátorem i live enginem)
-- ---------------------------------------------------------------------
CREATE TABLE matches (
    id                  BIGSERIAL PRIMARY KEY,
    external_api_id     VARCHAR(64),                  -- ID od sportovního data providera
    sport               VARCHAR(20) NOT NULL,         -- football / tennis / hockey / basketball
    league              VARCHAR(100),
    home_team           VARCHAR(100) NOT NULL,
    away_team           VARCHAR(100) NOT NULL,
    start_time          TIMESTAMPTZ NOT NULL,
    status              VARCHAR(20) DEFAULT 'scheduled', -- scheduled / live / finished
    home_score          SMALLINT DEFAULT 0,
    away_score          SMALLINT DEFAULT 0,
    current_minute      SMALLINT,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_matches_status ON matches(status);
CREATE INDEX idx_matches_sport_time ON matches(sport, start_time);

-- ---------------------------------------------------------------------
-- 3. TIKETY (Generátor tiketů) — hlavička kombinovaného tiketu
--
-- POZN.: appka NEPOUŽÍVÁ tabulku matches pro tikety — SelectionCandidate
-- nese match_id přímo jako ID z API-Football (ne interní serial), a appka
-- jména týmů ukládá denormalizovaně přímo na ticket_selections, ať appka
-- nemusí budovat a synchronizovat samostatnou tabulku zápasů, kterou
-- v kódu (zatím) nikde nepopuluje.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tickets (
    id                      BIGSERIAL PRIMARY KEY,
    user_id                 BIGINT NOT NULL,
    ticket_type             VARCHAR(20) NOT NULL CHECK (ticket_type IN ('safe','aggressive')),
    total_odds              NUMERIC(8,2) NOT NULL,
    combined_probability    NUMERIC(6,4) NOT NULL,        -- 0.0-1.0, NE procenta
    recommended_stake_pct   NUMERIC(6,2) NOT NULL DEFAULT 0,  -- % bankrollu, čtvrtinový Kelly
    status                  VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','won','lost')),
    live_alert              TEXT,                          -- viz _check_ticket_contradictions
    actual_stake_amount     NUMERIC(12,2),                 -- co uživatel REÁLNĚ vsadil
    actual_odds             NUMERIC(8,2),                  -- za jaký kurz to reálně vsadil
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at              TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);

-- ---------------------------------------------------------------------
-- 4. JEDNOTLIVÉ TIPY V TIKETU (umožňuje kombinaci trhů: výhra+over gólů+BTTS)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ticket_selections (
    id                   BIGSERIAL PRIMARY KEY,
    ticket_id            BIGINT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    match_id             BIGINT NOT NULL,                  -- ID z API-Football, NE interní serial
    home_team            VARCHAR(120) NOT NULL,
    away_team            VARCHAR(120) NOT NULL,
    market_type          VARCHAR(50) NOT NULL,             -- 'match_winner','over_goals','btts','over_cards'...
    selection            VARCHAR(50) NOT NULL,             -- 'home','draw','away','over_2.5','yes'...
    odds                 NUMERIC(8,2) NOT NULL,
    probability          NUMERIC(6,4) NOT NULL,            -- finální (tržní, pokud appka má kurzy, jinak model)
    model_probability    NUMERIC(6,4) NOT NULL DEFAULT 0,  -- appkin vlastní odhad, NEZÁVISLE na trhu
    market_probability   NUMERIC(6,4),                      -- de-vigovaná tržní pravděpodobnost, pokud appka má kurzy
    league                VARCHAR(120) DEFAULT '',
    kickoff_date          VARCHAR(20) DEFAULT '',
    reasoning              TEXT DEFAULT '',                 -- lidsky čitelné zdůvodnění výběru
    data_quality            TEXT DEFAULT ''                  -- které zdroje dat appka reálně sehnala
);

CREATE INDEX IF NOT EXISTS idx_selections_ticket ON ticket_selections(ticket_id);
CREATE INDEX IF NOT EXISTS idx_selections_match ON ticket_selections(match_id);

-- ---------------------------------------------------------------------
-- 5. ČASOVÁ ŘADA STATISTIK ZÁPASU (vstup pro Momentum Filter)
-- ---------------------------------------------------------------------
CREATE TABLE match_stats_timeline (
    id                      BIGSERIAL PRIMARY KEY,
    match_id                 BIGINT REFERENCES matches(id),
    minute                    SMALLINT NOT NULL,
    home_possession           SMALLINT,                -- %
    away_possession           SMALLINT,
    home_shots_on_target      SMALLINT DEFAULT 0,
    away_shots_on_target      SMALLINT DEFAULT 0,
    home_dangerous_attacks    SMALLINT DEFAULT 0,
    away_dangerous_attacks    SMALLINT DEFAULT 0,
    home_corners               SMALLINT DEFAULT 0,
    away_corners               SMALLINT DEFAULT 0,
    red_cards_home             SMALLINT DEFAULT 0,
    red_cards_away             SMALLINT DEFAULT 0,
    recorded_at                 TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_stats_match_minute ON match_stats_timeline(match_id, minute);

-- ---------------------------------------------------------------------
-- 6. LIVE SIGNÁLY (output Live Signal Engine)
-- ---------------------------------------------------------------------
CREATE TABLE live_signals (
    id                       BIGSERIAL PRIMARY KEY,
    match_id                  BIGINT REFERENCES matches(id),
    market_type               VARCHAR(50) NOT NULL,
    recommended_odds          NUMERIC(6,2),
    momentum_score_home       NUMERIC(5,2),
    momentum_score_away       NUMERIC(5,2),
    is_real_pressure          BOOLEAN,             -- true = skutečný tlak, false = falešné držení míče
    reasoning                  TEXT,                -- zdůvodnění analýzy pro uživatele
    recommended_stake_pct     NUMERIC(5,2),         -- doporučený vklad jako % bankrollu
    signal_type                VARCHAR(20) NOT NULL DEFAULT 'entry', -- entry / cashout / adjust
    trigger_event               VARCHAR(50),          -- 'momentum_shift','red_card','tempo_change', NULL
    sent_at                      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_live_signals_match ON live_signals(match_id, sent_at);

-- ---------------------------------------------------------------------
-- 7. HISTORIE SÁZEK / VÝSLEDKŮ (společná pro tikety i live signály)
-- ---------------------------------------------------------------------
CREATE TABLE bet_history (
    id                  BIGSERIAL PRIMARY KEY,
    user_id              BIGINT REFERENCES users(id),
    source_type           VARCHAR(20) NOT NULL CHECK (source_type IN ('ticket','live_signal')),
    source_id              BIGINT NOT NULL,        -- polymorfní FK -> tickets.id nebo live_signals.id
    stake_amount            NUMERIC(12,2),
    odds_at_placement        NUMERIC(6,2),
    result                    VARCHAR(20),           -- won/lost/cashed_out/void
    profit_loss               NUMERIC(12,2),
    closed_at                  TIMESTAMPTZ
);

CREATE INDEX idx_bet_history_user ON bet_history(user_id, closed_at);
