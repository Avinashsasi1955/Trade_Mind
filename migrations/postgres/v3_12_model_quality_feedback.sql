BEGIN;

CREATE TABLE IF NOT EXISTS model_quality_feedback(
    id BIGSERIAL PRIMARY KEY,
    audit_id BIGINT NOT NULL UNIQUE REFERENCES shadow_execution_audits(id) ON DELETE CASCADE,
    model_version TEXT NOT NULL,
    instrument_id BIGINT NOT NULL REFERENCES instrument_master(id),
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    instrument_type TEXT NOT NULL,
    signal_at TIMESTAMPTZ NOT NULL,
    exit_at TIMESTAMPTZ,
    side TEXT NOT NULL,
    quality_score NUMERIC(8,2) NOT NULL,
    quality_grade TEXT NOT NULL,
    net_pnl NUMERIC(24,10) NOT NULL,
    outcome_label INTEGER NOT NULL CHECK(outcome_label IN (0,1)),
    components JSONB NOT NULL,
    inclusion_reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_model_quality_feedback_model ON model_quality_feedback(model_version, signal_at DESC);
CREATE INDEX IF NOT EXISTS idx_model_quality_feedback_symbol ON model_quality_feedback(exchange, symbol, signal_at DESC);

INSERT INTO schema_migrations(version) VALUES('v3_12_model_quality_feedback') ON CONFLICT DO NOTHING;

COMMIT;
