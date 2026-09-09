BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE instrument_kind AS ENUM ('EQ','FUT','CE','PE','INDEX');
CREATE TYPE bar_interval AS ENUM ('1second','1minute','5minute','day');
CREATE TYPE order_side AS ENUM ('BUY','SELL');
CREATE TYPE execution_status AS ENUM ('CREATED','RISK_REJECTED','APPROVAL_PENDING','APPROVED','SUBMITTED','OPEN','PARTIAL','COMPLETE','REJECTED','CANCELLED');

CREATE TABLE instrument_master (
    id BIGSERIAL PRIMARY KEY,
    instrument_token BIGINT,
    exchange VARCHAR(10) NOT NULL CHECK (exchange IN ('NSE','BSE','NFO','BFO')),
    symbol VARCHAR(64) NOT NULL,
    underlying_symbol VARCHAR(64),
    isin VARCHAR(12),
    instrument_type instrument_kind NOT NULL,
    expiry DATE,
    strike NUMERIC(24,10),
    lot_size INTEGER NOT NULL DEFAULT 1 CHECK (lot_size > 0),
    tick_size NUMERIC(24,10) NOT NULL CHECK (tick_size > 0),
    is_fno_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    valid_to TIMESTAMPTZ,
    system_recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (exchange, symbol),
    CHECK ((instrument_type IN ('FUT','CE','PE') AND expiry IS NOT NULL) OR instrument_type IN ('EQ','INDEX')),
    CHECK ((instrument_type IN ('CE','PE') AND strike IS NOT NULL) OR instrument_type NOT IN ('CE','PE'))
);
CREATE INDEX idx_instrument_underlying_expiry ON instrument_master (underlying_symbol, expiry, strike);
CREATE INDEX idx_instrument_isin ON instrument_master (isin) WHERE isin IS NOT NULL;
CREATE UNIQUE INDEX idx_instrument_token_unique ON instrument_master(instrument_token) WHERE instrument_token IS NOT NULL AND instrument_token > 0;

CREATE TABLE live_market_bars (
    instrument_id BIGINT NOT NULL REFERENCES instrument_master(id),
    interval bar_interval NOT NULL,
    bar_time TIMESTAMPTZ NOT NULL,
    open_price NUMERIC(24,10) NOT NULL CHECK (open_price > 0),
    high_price NUMERIC(24,10) NOT NULL CHECK (high_price > 0),
    low_price NUMERIC(24,10) NOT NULL CHECK (low_price > 0),
    close_price NUMERIC(24,10) NOT NULL CHECK (close_price > 0),
    volume BIGINT NOT NULL DEFAULT 0 CHECK (volume >= 0),
    open_interest BIGINT CHECK (open_interest IS NULL OR open_interest >= 0),
    oi_change BIGINT,
    source VARCHAR(32) NOT NULL DEFAULT 'zerodha_kite',
    exchange_timestamp TIMESTAMPTZ,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (instrument_id, interval, bar_time),
    CHECK (high_price >= GREATEST(open_price, close_price, low_price)),
    CHECK (low_price <= LEAST(open_price, close_price, high_price))
);
CREATE INDEX idx_live_bars_time ON live_market_bars (bar_time DESC);

CREATE TABLE india_vix_history (
    observed_at TIMESTAMPTZ PRIMARY KEY,
    value NUMERIC(18,8) NOT NULL CHECK (value > 0),
    open_value NUMERIC(18,8), high_value NUMERIC(18,8), low_value NUMERIC(18,8),
    source VARCHAR(32) NOT NULL DEFAULT 'zerodha_kite',
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE account_equity_snapshots (
    id BIGSERIAL PRIMARY KEY,
    account_ref UUID NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    session_date DATE NOT NULL,
    opening_equity NUMERIC(24,10) NOT NULL CHECK (opening_equity > 0),
    current_equity NUMERIC(24,10) NOT NULL CHECK (current_equity >= 0),
    realised_pnl NUMERIC(24,10) NOT NULL DEFAULT 0,
    unrealised_pnl NUMERIC(24,10) NOT NULL DEFAULT 0,
    daily_drawdown_pct NUMERIC(12,8) GENERATED ALWAYS AS ((current_equity / opening_equity - 1) * 100) STORED,
    UNIQUE (account_ref, observed_at)
);

CREATE TABLE risk_control_state (
    account_ref UUID PRIMARY KEY,
    trading_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    kill_switch_active BOOLEAN NOT NULL DEFAULT TRUE,
    kill_reason TEXT NOT NULL DEFAULT 'Initial production lock',
    hard_daily_drawdown_pct NUMERIC(12,8) NOT NULL DEFAULT -7.32,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (hard_daily_drawdown_pct < 0)
);

CREATE OR REPLACE FUNCTION enforce_hard_daily_drawdown() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.daily_drawdown_pct <= COALESCE(
        (SELECT hard_daily_drawdown_pct FROM risk_control_state WHERE account_ref=NEW.account_ref), -7.32
    ) THEN
        INSERT INTO risk_control_state(account_ref,trading_enabled,kill_switch_active,kill_reason,updated_at)
        VALUES(NEW.account_ref,FALSE,TRUE,'Hard daily account drawdown breached',CURRENT_TIMESTAMP)
        ON CONFLICT(account_ref) DO UPDATE SET trading_enabled=FALSE,kill_switch_active=TRUE,
            kill_reason='Hard daily account drawdown breached',updated_at=CURRENT_TIMESTAMP;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_hard_daily_drawdown AFTER INSERT OR UPDATE ON account_equity_snapshots
FOR EACH ROW EXECUTE FUNCTION enforce_hard_daily_drawdown();

CREATE TABLE order_execution_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_ref UUID NOT NULL,
    client_order_id VARCHAR(64) NOT NULL UNIQUE,
    broker_order_id VARCHAR(32),
    exchange_order_id VARCHAR(32),
    instrument_id BIGINT NOT NULL REFERENCES instrument_master(id),
    side order_side NOT NULL,
    product VARCHAR(10) NOT NULL CHECK (product IN ('CNC','MIS','NRML')),
    order_type VARCHAR(10) NOT NULL CHECK (order_type IN ('LIMIT','SL')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    limit_price NUMERIC(24,10) NOT NULL CHECK (limit_price > 0),
    average_fill_price NUMERIC(24,10),
    filled_quantity INTEGER NOT NULL DEFAULT 0 CHECK (filled_quantity >= 0),
    status execution_status NOT NULL DEFAULT 'CREATED',
    model_version VARCHAR(128) NOT NULL,
    signal_probability NUMERIC(12,10) NOT NULL CHECK (signal_probability BETWEEN 0 AND 1),
    estimated_brokerage NUMERIC(24,10) NOT NULL DEFAULT 0,
    estimated_stt NUMERIC(24,10) NOT NULL DEFAULT 0,
    estimated_exchange_charges NUMERIC(24,10) NOT NULL DEFAULT 0,
    estimated_sebi_charges NUMERIC(24,10) NOT NULL DEFAULT 0,
    estimated_gst NUMERIC(24,10) NOT NULL DEFAULT 0,
    estimated_stamp_duty NUMERIC(24,10) NOT NULL DEFAULT 0,
    estimated_slippage NUMERIC(24,10) NOT NULL DEFAULT 0,
    actual_contract_note_fees NUMERIC(24,10),
    actual_slippage NUMERIC(24,10),
    contract_note_reconciled_at TIMESTAMPTZ,
    rejection_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_order_account_created ON order_execution_ledger(account_ref, created_at DESC);
CREATE INDEX idx_order_broker_id ON order_execution_ledger(broker_order_id) WHERE broker_order_id IS NOT NULL;

CREATE TABLE order_execution_events (
    id BIGSERIAL PRIMARY KEY,
    order_id UUID NOT NULL REFERENCES order_execution_ledger(id),
    event_type VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_order_events ON order_execution_events(order_id, occurred_at);

CREATE TABLE news_articles_v3 (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content_hash CHAR(64) NOT NULL UNIQUE,
    provider VARCHAR(32) NOT NULL,
    headline TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    source_name VARCHAR(128) NOT NULL,
    source_url TEXT,
    published_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    symbols TEXT[] NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_news_published ON news_articles_v3(published_at DESC);
CREATE INDEX idx_news_symbols ON news_articles_v3 USING GIN(symbols);

CREATE TABLE news_sentiment_scores (
    article_id UUID NOT NULL REFERENCES news_articles_v3(id),
    model_name VARCHAR(128) NOT NULL,
    positive_probability NUMERIC(12,10) NOT NULL,
    neutral_probability NUMERIC(12,10) NOT NULL,
    negative_probability NUMERIC(12,10) NOT NULL,
    scored_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(article_id, model_name)
);

CREATE TABLE ml_feature_observations (
    model_version VARCHAR(128) NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    feature_name VARCHAR(128) NOT NULL,
    feature_value NUMERIC(30,15) NOT NULL,
    PRIMARY KEY(model_version, observed_at, feature_name)
);
CREATE INDEX idx_feature_observation_window ON ml_feature_observations(model_version,feature_name,observed_at DESC);

CREATE TABLE model_drift_reports (
    id BIGSERIAL PRIMARY KEY,
    model_version VARCHAR(128) NOT NULL,
    feature_name VARCHAR(128) NOT NULL,
    psi NUMERIC(18,10) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK(status IN ('stable','watch','alert')),
    reference_start TIMESTAMPTZ NOT NULL,
    reference_end TIMESTAMPTZ NOT NULL,
    current_start TIMESTAMPTZ NOT NULL,
    current_end TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

COMMIT;
