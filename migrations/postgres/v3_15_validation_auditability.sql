BEGIN;

CREATE TABLE IF NOT EXISTS trade_candidate_audits (
    id BIGSERIAL PRIMARY KEY,
    observed_at TIMESTAMPTZ NOT NULL,
    model_version TEXT,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    instrument_id BIGINT REFERENCES instrument_master(id) ON DELETE SET NULL,
    instrument_type TEXT,
    signal INTEGER NOT NULL DEFAULT 0,
    probability NUMERIC(12,10),
    decision_price NUMERIC(20,10),
    selector_stage TEXT NOT NULL,
    accepted BOOLEAN NOT NULL DEFAULT FALSE,
    rejection_reason TEXT,
    chart_strategy TEXT,
    route TEXT,
    rr NUMERIC(12,4),
    expected_net_edge_bps NUMERIC(12,4),
    quality_score NUMERIC(12,4),
    selector_score NUMERIC(12,4),
    sector TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(model_version, instrument_id, observed_at, selector_stage, rejection_reason)
);

CREATE INDEX IF NOT EXISTS idx_trade_candidate_audits_observed
    ON trade_candidate_audits(observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_candidate_audits_symbol_time
    ON trade_candidate_audits(exchange, symbol, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_candidate_audits_reason_time
    ON trade_candidate_audits(rejection_reason, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_candidate_audits_accepted_time
    ON trade_candidate_audits(accepted, observed_at DESC);

CREATE TABLE IF NOT EXISTS shadow_session_daily_log (
    session_date DATE PRIMARY KEY,
    status TEXT NOT NULL,
    valid BOOLEAN NOT NULL DEFAULT FALSE,
    completed_sessions INTEGER NOT NULL DEFAULT 0,
    effective_completed_sessions INTEGER NOT NULL DEFAULT 0,
    live_one_minute_buckets INTEGER NOT NULL DEFAULT 0,
    live_five_minute_buckets INTEGER NOT NULL DEFAULT 0,
    live_instruments_seen INTEGER NOT NULL DEFAULT 0,
    repaired_one_minute_bars BIGINT NOT NULL DEFAULT 0,
    repaired_five_minute_bars BIGINT NOT NULL DEFAULT 0,
    predictions BIGINT NOT NULL DEFAULT 0,
    trades_taken BIGINT NOT NULL DEFAULT 0,
    closed_trades BIGINT NOT NULL DEFAULT 0,
    win_rate_pct NUMERIC(12,4) NOT NULL DEFAULT 0,
    net_pnl NUMERIC(20,6) NOT NULL DEFAULT 0,
    rejection_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    mistakes JSONB NOT NULL DEFAULT '{}'::jsonb,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_shadow_session_daily_log_status_date
    ON shadow_session_daily_log(status, session_date DESC);

CREATE INDEX IF NOT EXISTS idx_live_market_bars_source_interval_time
    ON live_market_bars(source, interval, bar_time DESC);
CREATE INDEX IF NOT EXISTS idx_live_market_bars_instrument_interval_time
    ON live_market_bars(instrument_id, interval, bar_time DESC);
CREATE INDEX IF NOT EXISTS idx_shadow_predictions_instrument_time
    ON shadow_predictions(instrument_id, timeframe, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_shadow_predictions_created
    ON shadow_predictions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_shadow_execution_audits_signal_status
    ON shadow_execution_audits(signal_at DESC, audit_status);
CREATE INDEX IF NOT EXISTS idx_shadow_execution_audits_instrument_open
    ON shadow_execution_audits(instrument_id, audit_status, net_pnl);
CREATE INDEX IF NOT EXISTS idx_monitoring_events_component_time
    ON monitoring_events(component, created_at DESC);

INSERT INTO schema_migrations(version) VALUES('v3_15_validation_auditability') ON CONFLICT DO NOTHING;

COMMIT;
