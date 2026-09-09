BEGIN;

CREATE TABLE IF NOT EXISTS auth_login_attempts (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL,
    succeeded SMALLINT NOT NULL DEFAULT 0 CHECK (succeeded IN (0,1)),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_auth_login_attempts_email_time
    ON auth_login_attempts(email,created_at DESC);

ALTER TABLE users ADD COLUMN IF NOT EXISTS disabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS cognito_sub TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_cognito_sub ON users(cognito_sub) WHERE cognito_sub IS NOT NULL;

INSERT INTO schema_migrations(version) VALUES('v3_5_security') ON CONFLICT DO NOTHING;

COMMIT;
