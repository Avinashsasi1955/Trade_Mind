BEGIN;

CREATE TABLE IF NOT EXISTS instrument_provider_keys (
    instrument_id BIGINT NOT NULL REFERENCES instrument_master(id) ON DELETE CASCADE,
    provider VARCHAR(32) NOT NULL,
    provider_key VARCHAR(128) NOT NULL,
    provider_token BIGINT NOT NULL,
    segment VARCHAR(32),
    mode_hint VARCHAR(16) NOT NULL DEFAULT 'ltpc' CHECK(mode_hint IN ('ltpc','quote','full','option_greeks','full_d30')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_synced_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(provider, provider_key),
    UNIQUE(provider, provider_token)
);

CREATE INDEX IF NOT EXISTS idx_provider_keys_instrument
    ON instrument_provider_keys(instrument_id, provider, is_active);

ALTER TABLE market_data_gaps ADD COLUMN IF NOT EXISTS provider VARCHAR(32) NOT NULL DEFAULT 'zerodha_kite';

INSERT INTO schema_migrations(version) VALUES('v3_9_market_data_providers') ON CONFLICT DO NOTHING;

COMMIT;
