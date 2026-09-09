BEGIN;

ALTER TABLE shadow_execution_audits
    ADD COLUMN IF NOT EXISTS stop_loss_price NUMERIC(24,10),
    ADD COLUMN IF NOT EXISTS take_profit_price NUMERIC(24,10),
    ADD COLUMN IF NOT EXISTS exit_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS exit_reason TEXT,
    ADD COLUMN IF NOT EXISTS fill_source TEXT NOT NULL DEFAULT 'DEPTH_OR_PENDING',
    ADD COLUMN IF NOT EXISTS mistake_tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS improvement_note TEXT;

CREATE INDEX IF NOT EXISTS idx_shadow_audits_open_paper
    ON shadow_execution_audits(signal_at, instrument_id)
    WHERE audit_status='RECONCILED' AND net_pnl IS NULL;

INSERT INTO schema_migrations(version) VALUES('v3_11_paper_trade_lifecycle') ON CONFLICT DO NOTHING;

COMMIT;
