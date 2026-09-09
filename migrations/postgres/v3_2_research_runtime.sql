BEGIN;

CREATE TABLE IF NOT EXISTS history_sync_coverage(
    instrument_id BIGINT NOT NULL REFERENCES instrument_master(id) ON DELETE CASCADE,
    interval bar_interval NOT NULL,
    first_timestamp TIMESTAMPTZ,
    last_timestamp TIMESTAMPTZ,
    bars BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('complete','failed')),
    error TEXT,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(instrument_id,interval)
);

CREATE TABLE IF NOT EXISTS history_ingestions(
    exchange TEXT NOT NULL,
    trade_date DATE NOT NULL,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    sha256 CHAR(64),
    byte_count BIGINT NOT NULL DEFAULT 0,
    row_count BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('complete','failed')),
    error TEXT,
    imported_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(exchange,trade_date,source)
);

CREATE TABLE IF NOT EXISTS corporate_actions(
    exchange TEXT NOT NULL,symbol TEXT NOT NULL,effective_date DATE NOT NULL,
    action_type TEXT NOT NULL CHECK(action_type IN ('split','bonus','dividend')),
    ratio_from NUMERIC(24,10) NOT NULL DEFAULT 1,ratio_to NUMERIC(24,10) NOT NULL DEFAULT 1,
    cash_amount NUMERIC(24,10) NOT NULL DEFAULT 0,source TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(exchange,symbol,effective_date,action_type)
);
CREATE TABLE IF NOT EXISTS corporate_action_ingestions(
    exchange TEXT NOT NULL,period_start DATE NOT NULL,period_end DATE NOT NULL,url TEXT NOT NULL,
    sha256 CHAR(64) NOT NULL,row_count BIGINT NOT NULL,status TEXT NOT NULL,imported_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(exchange,period_start,period_end)
);
CREATE TABLE IF NOT EXISTS universe_membership(
    exchange TEXT NOT NULL,symbol TEXT NOT NULL,valid_from DATE NOT NULL,valid_to DATE,
    status TEXT NOT NULL,source TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(exchange,symbol,valid_from)
);
CREATE TABLE IF NOT EXISTS feature_rows(
    exchange TEXT NOT NULL,symbol TEXT NOT NULL,"timestamp" TIMESTAMPTZ NOT NULL,
    feature_set TEXT NOT NULL,features JSONB NOT NULL,label INTEGER,label_return NUMERIC(30,15),
    source_hash CHAR(64) NOT NULL,created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(exchange,symbol,"timestamp",feature_set)
);
CREATE INDEX IF NOT EXISTS idx_features_set_time ON feature_rows(feature_set,"timestamp");
CREATE INDEX IF NOT EXISTS idx_features_liquidity ON feature_rows(((features->>'average_daily_value_20d')::numeric))
    WHERE features ? 'average_daily_value_20d';
CREATE TABLE IF NOT EXISTS model_versions(
    id BIGSERIAL PRIMARY KEY,model_name TEXT NOT NULL,version TEXT UNIQUE NOT NULL,
    feature_set TEXT NOT NULL,algorithm TEXT NOT NULL,payload JSONB NOT NULL,
    training_start TIMESTAMPTZ,training_end TIMESTAMPTZ,training_samples BIGINT NOT NULL,
    metrics JSONB NOT NULL,status TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS validation_runs(
    id BIGSERIAL PRIMARY KEY,model_version TEXT NOT NULL,validation_type TEXT NOT NULL,
    period_start TIMESTAMPTZ,period_end TIMESTAMPTZ,symbols INTEGER NOT NULL,trades INTEGER NOT NULL,
    metrics JSONB NOT NULL,created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_predictions(
    id BIGSERIAL PRIMARY KEY,model_version TEXT NOT NULL,exchange TEXT NOT NULL,symbol TEXT NOT NULL,
    "timestamp" TIMESTAMPTZ NOT NULL,probability NUMERIC(12,10) NOT NULL,signal INTEGER NOT NULL,
    realised_return NUMERIC(30,15),status TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(model_version,exchange,symbol,"timestamp")
);
CREATE TABLE IF NOT EXISTS drift_reports(
    id BIGSERIAL PRIMARY KEY,model_version TEXT NOT NULL,feature TEXT NOT NULL,
    reference_mean NUMERIC(30,15) NOT NULL,current_mean NUMERIC(30,15) NOT NULL,
    standardised_shift NUMERIC(30,15) NOT NULL,status TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL
);

INSERT INTO schema_migrations(version) VALUES('v3_2_research_runtime') ON CONFLICT DO NOTHING;
COMMIT;
