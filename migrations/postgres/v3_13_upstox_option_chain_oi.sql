BEGIN;

CREATE TABLE IF NOT EXISTS option_chain_oi_snapshots (
    id BIGSERIAL PRIMARY KEY,
    instrument_id BIGINT NOT NULL REFERENCES instrument_master(id) ON DELETE CASCADE,
    underlying_instrument_id BIGINT REFERENCES instrument_master(id) ON DELETE SET NULL,
    provider VARCHAR(32) NOT NULL DEFAULT 'upstox_v2_option_chain',
    observed_at TIMESTAMPTZ NOT NULL,
    exchange VARCHAR(10) NOT NULL,
    symbol VARCHAR(64) NOT NULL,
    underlying_symbol VARCHAR(64) NOT NULL,
    expiry DATE NOT NULL,
    strike NUMERIC(24,10) NOT NULL,
    option_type instrument_kind NOT NULL CHECK(option_type IN ('CE','PE')),
    last_price NUMERIC(24,10),
    best_bid NUMERIC(24,10),
    best_ask NUMERIC(24,10),
    bid_quantity BIGINT,
    ask_quantity BIGINT,
    volume BIGINT,
    open_interest BIGINT CHECK(open_interest IS NULL OR open_interest >= 0),
    previous_open_interest BIGINT CHECK(previous_open_interest IS NULL OR previous_open_interest >= 0),
    oi_change BIGINT,
    implied_volatility NUMERIC(18,8),
    delta NUMERIC(18,8),
    gamma NUMERIC(18,8),
    theta NUMERIC(18,8),
    vega NUMERIC(18,8),
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(instrument_id, observed_at, provider)
);

CREATE INDEX IF NOT EXISTS idx_option_chain_oi_snapshots_underlying
    ON option_chain_oi_snapshots(underlying_symbol, expiry, strike, option_type, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_option_chain_oi_snapshots_instrument
    ON option_chain_oi_snapshots(instrument_id, observed_at DESC);

INSERT INTO schema_migrations(version) VALUES('v3_13_upstox_option_chain_oi') ON CONFLICT DO NOTHING;

COMMIT;
