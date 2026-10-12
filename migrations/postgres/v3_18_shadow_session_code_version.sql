-- ==============================================================================
-- v3.18 Shadow Session Daily Log Code Version Tagging
-- Adds code_version column to shadow_session_daily_log
-- ==============================================================================

ALTER TABLE shadow_session_daily_log
ADD COLUMN IF NOT EXISTS code_version VARCHAR(64);

INSERT INTO schema_migrations(version) VALUES('v3_18_shadow_session_code_version') ON CONFLICT DO NOTHING;
