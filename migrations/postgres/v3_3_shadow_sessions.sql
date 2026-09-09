BEGIN;
CREATE TABLE IF NOT EXISTS forward_shadow_sessions(
    session_date DATE PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('STARTED','COMPLETE','REJECTED')),
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    one_minute_bars BIGINT NOT NULL DEFAULT 0,
    five_minute_bars BIGINT NOT NULL DEFAULT 0,
    instruments_seen BIGINT NOT NULL DEFAULT 0,
    vix_observations BIGINT NOT NULL DEFAULT 0,
    oi_observations BIGINT NOT NULL DEFAULT 0,
    predictions BIGINT NOT NULL DEFAULT 0,
    news_articles BIGINT NOT NULL DEFAULT 0,
    critical_errors BIGINT NOT NULL DEFAULT 0,
    live_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    execution_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    rejection_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK(NOT live_eligible),
    CHECK(NOT execution_enabled)
);
CREATE INDEX IF NOT EXISTS idx_shadow_sessions_status_date ON forward_shadow_sessions(status,session_date);
INSERT INTO schema_migrations(version) VALUES('v3_3_shadow_sessions') ON CONFLICT DO NOTHING;
COMMIT;
