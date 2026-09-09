BEGIN;

CREATE TABLE IF NOT EXISTS adaptive_trade_rewards(
    id BIGSERIAL PRIMARY KEY,
    audit_id BIGINT NOT NULL UNIQUE REFERENCES shadow_execution_audits(id) ON DELETE CASCADE,
    model_version TEXT NOT NULL,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    underlying_symbol TEXT NOT NULL,
    instrument_type TEXT NOT NULL,
    side TEXT NOT NULL,
    strategy TEXT NOT NULL,
    regime TEXT NOT NULL,
    hour_bucket SMALLINT NOT NULL,
    quality_score NUMERIC(8,2) NOT NULL,
    reward_points NUMERIC(12,4) NOT NULL,
    net_pnl NUMERIC(24,10) NOT NULL,
    exit_reason TEXT,
    mistake_tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    signature_hash TEXT NOT NULL,
    learned_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_adaptive_rewards_signature
    ON adaptive_trade_rewards(signature_hash, learned_at DESC);
CREATE INDEX IF NOT EXISTS idx_adaptive_rewards_underlying
    ON adaptive_trade_rewards(underlying_symbol, instrument_type, side, learned_at DESC);
CREATE INDEX IF NOT EXISTS idx_adaptive_rewards_strategy
    ON adaptive_trade_rewards(strategy, regime, learned_at DESC);

CREATE TABLE IF NOT EXISTS adaptive_trade_guardrails(
    trade_date DATE PRIMARY KEY,
    reward_points NUMERIC(14,4) NOT NULL DEFAULT 0,
    trades_scored INTEGER NOT NULL DEFAULT 0,
    hard_kill_triggered BOOLEAN NOT NULL DEFAULT FALSE,
    kill_reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO schema_migrations(version) VALUES('v3_14_adaptive_trade_intelligence') ON CONFLICT DO NOTHING;

COMMIT;
