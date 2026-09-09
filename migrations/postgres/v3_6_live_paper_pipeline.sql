BEGIN;

CREATE TABLE IF NOT EXISTS live_feature_snapshots(
    model_version TEXT NOT NULL,
    instrument_id BIGINT NOT NULL REFERENCES instrument_master(id),
    observed_at TIMESTAMPTZ NOT NULL,
    timeframe bar_interval NOT NULL,
    feature_set TEXT NOT NULL,
    features JSONB NOT NULL,
    feature_hash CHAR(64) NOT NULL,
    source_bar_time TIMESTAMPTZ NOT NULL,
    causal_watermark TIMESTAMPTZ NOT NULL,
    decision_price NUMERIC(24,10) NOT NULL CHECK(decision_price>0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(model_version,instrument_id,timeframe,observed_at),
    CHECK(source_bar_time<=causal_watermark)
);
CREATE INDEX IF NOT EXISTS idx_live_features_observed
    ON live_feature_snapshots(observed_at DESC,instrument_id);
CREATE INDEX IF NOT EXISTS idx_live_bars_interval_source_time
    ON live_market_bars(interval,source,bar_time DESC);

ALTER TABLE shadow_predictions ADD COLUMN IF NOT EXISTS instrument_id BIGINT REFERENCES instrument_master(id);
ALTER TABLE shadow_predictions ADD COLUMN IF NOT EXISTS timeframe bar_interval;
ALTER TABLE shadow_predictions ADD COLUMN IF NOT EXISTS feature_hash CHAR(64);
ALTER TABLE shadow_predictions ADD COLUMN IF NOT EXISTS decision_price NUMERIC(24,10);

CREATE TABLE IF NOT EXISTS market_data_gaps(
    id BIGSERIAL PRIMARY KEY,
    stream_id TEXT NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_tick_at TIMESTAMPTZ,
    recovered_at TIMESTAMPTZ,
    affected_tokens INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('OPEN','RECOVERED')),
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_market_gaps_open ON market_data_gaps(status,detected_at DESC);

CREATE TABLE IF NOT EXISTS exchange_trading_calendar(
    exchange TEXT NOT NULL CHECK(exchange IN ('NSE','BSE')),
    session_date DATE NOT NULL,
    session_status TEXT NOT NULL CHECK(session_status IN ('OPEN','CLOSED','SPECIAL')),
    opens_at TIME,
    closes_at TIME,
    source TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(exchange,session_date),
    CHECK(session_status='CLOSED' OR (opens_at IS NOT NULL AND closes_at IS NOT NULL AND opens_at<closes_at))
);

INSERT INTO schema_migrations(version) VALUES('v3_6_live_paper_pipeline') ON CONFLICT DO NOTHING;
COMMIT;
