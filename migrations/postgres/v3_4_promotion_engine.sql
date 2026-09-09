BEGIN;
CREATE TABLE IF NOT EXISTS shadow_execution_audits(
    id BIGSERIAL PRIMARY KEY,
    model_version TEXT NOT NULL,
    instrument_id BIGINT NOT NULL REFERENCES instrument_master(id),
    signal_at TIMESTAMPTZ NOT NULL,
    side order_side NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity>0),
    signal_probability NUMERIC(12,10) NOT NULL CHECK(signal_probability BETWEEN 0 AND 1),
    decision_price NUMERIC(24,10) NOT NULL CHECK(decision_price>0),
    best_bid NUMERIC(24,10),best_ask NUMERIC(24,10),bid_quantity BIGINT,ask_quantity BIGINT,
    theoretical_fill_price NUMERIC(24,10),one_tick_penalty NUMERIC(24,10),estimated_fees NUMERIC(24,10),
    realised_exit_price NUMERIC(24,10),net_pnl NUMERIC(24,10),
    cost_reconciled BOOLEAN NOT NULL DEFAULT FALSE,
    instrument_execution_verified BOOLEAN NOT NULL DEFAULT FALSE,
    audit_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(audit_status IN ('PENDING','RECONCILED','REJECTED')),
    rejection_reason TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(model_version,instrument_id,signal_at,side)
);
CREATE INDEX IF NOT EXISTS idx_shadow_audits_session ON shadow_execution_audits(((signal_at AT TIME ZONE 'Asia/Kolkata')::date),audit_status);

CREATE TABLE IF NOT EXISTS system_promotion_ledger(
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    completed_sessions INTEGER NOT NULL DEFAULT 0,
    rolling_profit_factor NUMERIC(18,8),
    max_drawdown_pct NUMERIC(12,8),
    untouched_log_loss NUMERIC(18,10),
    reconciled_cost_sessions INTEGER NOT NULL DEFAULT 0,
    verified_derivative_executions INTEGER NOT NULL DEFAULT 0,
    sessions_gate BOOLEAN NOT NULL DEFAULT FALSE,
    performance_gate BOOLEAN NOT NULL DEFAULT FALSE,
    log_loss_gate BOOLEAN NOT NULL DEFAULT FALSE,
    costs_gate BOOLEAN NOT NULL DEFAULT FALSE,
    instrument_gate BOOLEAN NOT NULL DEFAULT FALSE,
    live_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(NOT live_eligible OR (completed_sessions>=90 AND rolling_profit_factor>=1.20 AND max_drawdown_pct>=-7.32
        AND untouched_log_loss<0.693 AND reconciled_cost_sessions>=20 AND verified_derivative_executions>=20
        AND sessions_gate AND performance_gate AND log_loss_gate AND costs_gate AND instrument_gate))
);
INSERT INTO system_promotion_ledger(singleton) VALUES(TRUE) ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION block_unpromoted_trading() RETURNS TRIGGER AS $$
BEGIN
  IF NEW.trading_enabled AND NOT EXISTS(SELECT 1 FROM system_promotion_ledger WHERE singleton AND live_eligible) THEN
    RAISE EXCEPTION 'Production execution blocked: promotion ledger is not eligible';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_block_unpromoted_trading ON risk_control_state;
CREATE TRIGGER trg_block_unpromoted_trading BEFORE INSERT OR UPDATE OF trading_enabled ON risk_control_state
FOR EACH ROW EXECUTE FUNCTION block_unpromoted_trading();

INSERT INTO schema_migrations(version) VALUES('v3_4_promotion_engine') ON CONFLICT DO NOTHING;
COMMIT;
