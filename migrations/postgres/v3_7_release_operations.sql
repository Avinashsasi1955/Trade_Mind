BEGIN;

CREATE TABLE IF NOT EXISTS operational_preflight_runs(
    id BIGSERIAL PRIMARY KEY,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    session_date DATE NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PASS','BLOCKED')),
    checks JSONB NOT NULL,
    blockers JSONB NOT NULL DEFAULT '[]'::jsonb,
    orders_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    CHECK(NOT orders_allowed)
);
CREATE INDEX IF NOT EXISTS idx_preflight_session_time ON operational_preflight_runs(session_date,checked_at DESC);

CREATE TABLE IF NOT EXISTS model_release_manifests(
    model_version TEXT PRIMARY KEY REFERENCES model_versions(version),
    released_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    feature_set TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    training_samples BIGINT NOT NULL,
    holdout_log_loss NUMERIC(18,10),
    artifact_sha256 CHAR(64) NOT NULL,
    key_fingerprint CHAR(16) NOT NULL,
    signature_verified BOOLEAN NOT NULL,
    promotion_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    manifest JSONB NOT NULL,
    CHECK(NOT promotion_eligible OR signature_verified)
);

INSERT INTO schema_migrations(version) VALUES('v3_7_release_operations') ON CONFLICT DO NOTHING;
COMMIT;
