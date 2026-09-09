BEGIN;

ALTER TABLE system_promotion_ledger
    ADD COLUMN IF NOT EXISTS cost_evidence_source TEXT NOT NULL DEFAULT 'NONE',
    ADD COLUMN IF NOT EXISTS instrument_evidence_source TEXT NOT NULL DEFAULT 'NONE';

ALTER TABLE system_promotion_ledger DROP CONSTRAINT IF EXISTS chk_real_cost_evidence;
ALTER TABLE system_promotion_ledger ADD CONSTRAINT chk_real_cost_evidence CHECK(
    NOT costs_gate OR (reconciled_cost_sessions>=20 AND cost_evidence_source='ZERODHA_CONTRACT_NOTE')
);
ALTER TABLE system_promotion_ledger DROP CONSTRAINT IF EXISTS chk_derivative_evidence;
ALTER TABLE system_promotion_ledger ADD CONSTRAINT chk_derivative_evidence CHECK(
    NOT instrument_gate OR (verified_derivative_executions>=20 AND instrument_evidence_source='BROKER_CONNECTED_SHADOW')
);

INSERT INTO schema_migrations(version) VALUES('v3_8_model_policy_evidence') ON CONFLICT DO NOTHING;
COMMIT;
