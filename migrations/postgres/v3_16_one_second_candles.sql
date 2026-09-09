BEGIN;

ALTER TYPE bar_interval ADD VALUE IF NOT EXISTS '1second' BEFORE '1minute';

COMMIT;

BEGIN;

CREATE INDEX IF NOT EXISTS idx_live_market_bars_1second_latest
    ON live_market_bars(instrument_id, bar_time DESC)
    WHERE interval='1second';

CREATE INDEX IF NOT EXISTS idx_live_market_bars_source_1second_time
    ON live_market_bars(source, bar_time DESC)
    WHERE interval='1second';

INSERT INTO schema_migrations(version) VALUES('v3_16_one_second_candles') ON CONFLICT DO NOTHING;

COMMIT;
