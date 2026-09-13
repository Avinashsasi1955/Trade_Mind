-- ==============================================================================
-- v3.17 Dual-Mode (Intraday vs. Swing) & Agentic Multi-Agent Ledger
-- Adds trade_mode, reasoning_chain, and multi-day lifecycle tracking
-- ==============================================================================

-- 1. Add trade_mode to shadow_execution_audits
ALTER TABLE shadow_execution_audits 
ADD COLUMN IF NOT EXISTS trade_mode VARCHAR(20) DEFAULT 'INTRADAY',
ADD COLUMN IF NOT EXISTS reasoning_chain JSONB,
ADD COLUMN IF NOT EXISTS holding_days INT DEFAULT 0,
ADD COLUMN IF NOT EXISTS max_holding_days INT DEFAULT 5,
ADD COLUMN IF NOT EXISTS spread_partner_id BIGINT REFERENCES shadow_execution_audits(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_shadow_execution_trade_mode 
ON shadow_execution_audits(trade_mode);

-- 2. Add trade_mode and agent_deliberation to trade_candidate_audits
ALTER TABLE trade_candidate_audits
ADD COLUMN IF NOT EXISTS trade_mode VARCHAR(20) DEFAULT 'INTRADAY',
ADD COLUMN IF NOT EXISTS agent_deliberation JSONB;

CREATE INDEX IF NOT EXISTS idx_trade_candidate_trade_mode
ON trade_candidate_audits(trade_mode);

-- 3. Create table for Sentinel Watcher and Agentic Orchestrator health logs
CREATE TABLE IF NOT EXISTS agentic_sentinel_logs (
    id BIGSERIAL PRIMARY KEY,
    component VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL,
    checks_passed INT DEFAULT 0,
    checks_failed INT DEFAULT 0,
    details JSONB,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agentic_sentinel_created 
ON agentic_sentinel_logs(created_at DESC);
