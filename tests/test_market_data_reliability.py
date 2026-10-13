"""Deterministic unit and integration tests for Phase 3: Market-Data Reliability.

Covers:
1. Canonical Data Provenance taxonomy & exact token matching (F-05).
   - Preserves upstox_v3, zerodha_kite, bhavcopy, etc.
   - Prevents substring traps ("delivery", "delivery_feed") from being classified as live.
   - Fails closed on unknown labels.
2. Broker-independent MarketDataFeed ABC and UpstoxMarketDataFeed adapter.
3. Fail-closed entry price validation (F-01, F-04).
   - Rejects missing timestamp (observed_at=None) with PRICE_OBSERVED_AT_TIMESTAMP_MISSING.
   - Rejects malformed timestamps with INVALID_OBSERVED_AT_TIMESTAMP.
   - Rejects zero, negative, NaN, Inf, stale, synthetic, bhavcopy, unhealthy feed.
4. Synthetic fallback isolation (F-04).
   - Categorically prohibited in production and staging even with NIVESH_ALLOW_SYNTHETIC_MARKET_DATA=1.
   - Allowed only in offline test/dev environments.
5. Risk engine market-data reliability integration (F-01, F-03, F-04).
   - Rejects missing quote timestamp at risk level.
   - Rejects when DATABASE_URL is missing in production/staging with MARKET_DATA_STORAGE_UNAVAILABLE.
   - Prohibits synthetic entries in production/staging regardless of override.
6. Option contract validation with independent provenance and timestamp (F-02).
   - _nearest_option_target selects option's own source and bar_time.
   - Fresh underlying + stale option contract is rejected (PRICE_STALE).
   - Option entry path never falls back to underlying index source or timestamp.
7. Preserved execution fuses & disabled live order submission.
"""
from __future__ import annotations

import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from backend.config import LIVE_TRADING_ENABLED
from backend.market import (
    INDICES,
    is_synthetic_market_data_allowed,
    market_snapshot,
    price_map as synthetic_price_map,
)
from backend.market_data_feed import (
    ENTRY_ELIGIBLE_PROVENANCES,
    DataProvenance,
    MarketDataFeed,
    UpstoxMarketDataFeed,
    classify_provenance,
    validate_market_data_for_entry,
    validate_quote_timestamp,
)
from backend.risk_engine import RiskEngine


class TestCanonicalProvenanceExactMatching:
    """Verifies §4.A and Fix F-05: Exact token matching & canonical provenance taxonomy."""

    def test_canonical_enum_tiers(self):
        assert DataProvenance.LIVE_STREAM == "LIVE_STREAM"
        assert DataProvenance.REST_BACKFILL == "REST_BACKFILL"
        assert DataProvenance.HISTORICAL_BHAVCOPY == "HISTORICAL_BHAVCOPY"
        assert DataProvenance.CACHED == "CACHED"
        assert DataProvenance.SYNTHETIC_FIXTURE == "SYNTHETIC_FIXTURE"

    @pytest.mark.parametrize(
        "label, expected",
        [
            # Supported live stream labels
            ("upstox_v3", DataProvenance.LIVE_STREAM),
            ("zerodha_kite", DataProvenance.LIVE_STREAM),
            ("live_stream", DataProvenance.LIVE_STREAM),
            ("upstox_live", DataProvenance.LIVE_STREAM),
            ("kite_live", DataProvenance.LIVE_STREAM),
            ("live_feed", DataProvenance.LIVE_STREAM),
            ("live", DataProvenance.LIVE_STREAM),
            # Mixed-case live labels
            ("UpStox_V3", DataProvenance.LIVE_STREAM),
            ("ZERODHA_KITE", DataProvenance.LIVE_STREAM),
            ("LIVE_STREAM", DataProvenance.LIVE_STREAM),
            ("Upstox_Live", DataProvenance.LIVE_STREAM),
            # Rest backfill labels
            ("upstox_rest_5m", DataProvenance.REST_BACKFILL),
            ("upstox_rest_intraday", DataProvenance.REST_BACKFILL),
            ("upstox_rest_history", DataProvenance.REST_BACKFILL),
            ("kite_gap_backfill", DataProvenance.REST_BACKFILL),
            ("gap_repair_finalizer", DataProvenance.REST_BACKFILL),
            ("UPSTOX_REST_5M", DataProvenance.REST_BACKFILL),
            # Historical Bhavcopy labels
            ("official_exchange_bhavcopy", DataProvenance.HISTORICAL_BHAVCOPY),
            ("bhavcopy", DataProvenance.HISTORICAL_BHAVCOPY),
            ("nse_bhavcopy", DataProvenance.HISTORICAL_BHAVCOPY),
            ("OFFICIAL_EXCHANGE_BHAVCOPY", DataProvenance.HISTORICAL_BHAVCOPY),
            # Cached labels
            ("redis_tick", DataProvenance.CACHED),
            ("cached", DataProvenance.CACHED),
            ("nivesh:ticks:latest", DataProvenance.CACHED),
            ("market", DataProvenance.CACHED),
            # Synthetic / fixture labels
            ("synthetic", DataProvenance.SYNTHETIC_FIXTURE),
            ("yahoo_sample", DataProvenance.SYNTHETIC_FIXTURE),
            ("market_snapshot_fallback", DataProvenance.SYNTHETIC_FIXTURE),
            # F-05: Substring traps containing 'live' must NOT be classified as LIVE_STREAM
            ("delivery", DataProvenance.SYNTHETIC_FIXTURE),
            ("delivery_feed", DataProvenance.SYNTHETIC_FIXTURE),
            ("delivery_order", DataProvenance.SYNTHETIC_FIXTURE),
            ("DELIVERY", DataProvenance.SYNTHETIC_FIXTURE),
            ("relive", DataProvenance.SYNTHETIC_FIXTURE),
            ("alive", DataProvenance.SYNTHETIC_FIXTURE),
            ("lively_feed", DataProvenance.SYNTHETIC_FIXTURE),
            # Unknown labels containing familiar tokens must NOT be trusted
            ("evil_live", DataProvenance.SYNTHETIC_FIXTURE),
            ("untrusted_stream", DataProvenance.SYNTHETIC_FIXTURE),
            ("unknown_vendor", DataProvenance.SYNTHETIC_FIXTURE),
            ("custom_data_feed", DataProvenance.SYNTHETIC_FIXTURE),
            ("", DataProvenance.SYNTHETIC_FIXTURE),
            (None, DataProvenance.SYNTHETIC_FIXTURE),
        ],
    )
    def test_classify_provenance_mapping(self, label, expected):
        assert classify_provenance(label) == expected


class TestMarketDataFeedAbstraction:
    """Verifies §4.B: Common feed interface and Upstox adapter."""

    def test_market_data_feed_is_abstract(self):
        class IncompleteFeed(MarketDataFeed):
            pass

        with pytest.raises(TypeError):
            IncompleteFeed()

    def test_upstox_feed_adapter_lifecycle_and_health(self):
        adapter = UpstoxMarketDataFeed(access_token="mock_token")
        assert adapter.provenance == DataProvenance.LIVE_STREAM

        # Initially unstarted/unconnected -> unhealthy
        assert not adapter.is_healthy
        status = adapter.health_status()
        assert status["provider"] == "upstox_v3"
        assert not status["is_healthy"]
        assert status["provenance"] == "LIVE_STREAM"

        # Mock connected stream with fresh heartbeat
        adapter._stream.ws = MagicMock()
        adapter._stream.last_heartbeat_at = time.time()
        assert adapter.is_healthy

        # Mock stale heartbeat (> 60 seconds ago) -> must fail health check
        adapter._stream.last_heartbeat_at = time.time() - 65.0
        assert not adapter.is_healthy

        # Mock disconnected ws -> must fail health check even if heartbeat was recent
        adapter._stream.last_heartbeat_at = time.time()
        adapter._stream.ws = None
        assert not adapter.is_healthy


class TestFailClosedPriceValidation:
    """Verifies §4.C and Fix F-01: Fail-closed price validation for new trade entry authorization."""

    def test_accepts_valid_fresh_live_quote(self):
        ref_time = datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc)
        observed = ref_time - timedelta(seconds=10)
        valid, reason = validate_market_data_for_entry(
            price=2990.50,
            observed_at=observed,
            source="upstox_v3",
            max_age_seconds=30.0,
            feed_healthy=True,
            reference_time=ref_time,
        )
        assert valid
        assert reason is None

    def test_rejects_missing_quote_price(self):
        valid, reason = validate_market_data_for_entry(
            price=None,
            observed_at=datetime.now(timezone.utc),
            source="upstox_v3",
        )
        assert not valid
        assert reason == "PRICE_MISSING"

    @pytest.mark.parametrize("invalid_price", [0.0, -1.0, -999.5, "abc", float("nan"), float("inf"), float("-inf")])
    def test_rejects_non_positive_and_non_finite_prices(self, invalid_price):
        valid, reason = validate_market_data_for_entry(
            price=invalid_price,
            observed_at=datetime.now(timezone.utc),
            source="upstox_v3",
        )
        assert not valid
        assert "PRICE" in reason

    def test_rejects_missing_observed_at_timestamp_by_default(self):
        """Fix F-01: observed_at=None must fail closed with PRICE_OBSERVED_AT_TIMESTAMP_MISSING."""
        valid, reason = validate_market_data_for_entry(
            price=2990.50,
            observed_at=None,
            source="upstox_v3",
        )
        assert not valid
        assert reason == "PRICE_OBSERVED_AT_TIMESTAMP_MISSING"

    def test_rejects_blank_observed_at_timestamp(self):
        """Fix F-01: blank string timestamp must fail closed."""
        valid, reason = validate_market_data_for_entry(
            price=2990.50,
            observed_at="   ",
            source="upstox_v3",
        )
        assert not valid
        assert reason == "PRICE_OBSERVED_AT_TIMESTAMP_MISSING"

    @pytest.mark.parametrize("malformed", ["not-a-date", "invalid_iso_time", "2026-99-99T99:99:99"])
    def test_rejects_malformed_observed_at_timestamp(self, malformed):
        """Fix F-01: malformed timestamps must fail closed."""
        valid, reason = validate_market_data_for_entry(
            price=2990.50,
            observed_at=malformed,
            source="upstox_v3",
        )
        assert not valid
        assert "INVALID_OBSERVED_AT_TIMESTAMP" in reason

    def test_rejects_stale_quote_exceeding_threshold(self):
        ref_time = datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc)
        observed = ref_time - timedelta(seconds=35)  # 35s old > 30s threshold
        valid, reason = validate_market_data_for_entry(
            price=2450.0,
            observed_at=observed,
            source="upstox_v3",
            max_age_seconds=30.0,
            reference_time=ref_time,
        )
        assert not valid
        assert "PRICE_STALE" in reason

    def test_rejects_future_timestamp(self):
        ref_time = datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc)
        observed = ref_time + timedelta(seconds=15)  # 15s in the future
        valid, reason = validate_market_data_for_entry(
            price=2450.0,
            observed_at=observed,
            source="upstox_v3",
            reference_time=ref_time,
        )
        assert not valid
        assert "PRICE_TIMESTAMP_IN_FUTURE" in reason

    def test_rejects_synthetic_source_for_entry(self):
        valid, reason = validate_market_data_for_entry(
            price=2450.0,
            observed_at=datetime.now(timezone.utc),
            source="synthetic",
            allow_synthetic=False,
        )
        assert not valid
        assert "SYNTHETIC_DATA_PROHIBITED_FOR_ENTRY" in reason

    def test_rejects_delivery_substring_label_as_synthetic(self):
        """Fix F-05: delivery is classified as SYNTHETIC_FIXTURE and prohibited for live entry."""
        valid, reason = validate_market_data_for_entry(
            price=2450.0,
            observed_at=datetime.now(timezone.utc),
            source="delivery",
            allow_synthetic=False,
        )
        assert not valid
        assert "SYNTHETIC_DATA_PROHIBITED_FOR_ENTRY" in reason

    def test_rejects_historical_bhavcopy_masquerading_as_live_entry(self):
        valid, reason = validate_market_data_for_entry(
            price=2450.0,
            observed_at=datetime.now(timezone.utc),
            source="official_exchange_bhavcopy",
        )
        assert not valid
        assert "HISTORICAL_BHAVCOPY_CANNOT_MASQUERADE_AS_LIVE_ENTRY" in reason

    def test_rejects_unhealthy_feed(self):
        valid, reason = validate_market_data_for_entry(
            price=2450.0,
            observed_at=datetime.now(timezone.utc),
            source="upstox_v3",
            feed_healthy=False,
        )
        assert not valid
        assert reason == "FEED_UNHEALTHY_OR_DISCONNECTED"

    def test_explicit_synthetic_override_for_offline_tests(self):
        valid, reason = validate_market_data_for_entry(
            price=2450.0,
            observed_at=datetime.now(timezone.utc),
            source="synthetic",
            allow_synthetic=True,
        )
        assert valid
        assert reason is None


class TestSyntheticFallbackIsolation:
    """Verifies §4.D and Fix F-04: Synthetic fallback isolation in production and staging."""

    @pytest.mark.parametrize("env", ["production", "staging", "PRODUCTION", "STAGING"])
    def test_production_and_staging_categorically_prohibit_synthetic_even_with_override(self, env):
        """Fix F-04: production and staging must NEVER permit synthetic data, even if NIVESH_ALLOW_SYNTHETIC_MARKET_DATA=1."""
        with patch.dict(os.environ, {"ENVIRONMENT": env, "NIVESH_ALLOW_SYNTHETIC_MARKET_DATA": "1"}):
            assert not is_synthetic_market_data_allowed()
            # market_snapshot() remains available for non-executable research/display and does not crash
            snapshot_data = market_snapshot()
            assert len(snapshot_data) > 0
            assert snapshot_data[0]["is_synthetic"] is True
            valid, reason = validate_market_data_for_entry(
                price=2450.0,
                observed_at=datetime.now(timezone.utc),
                source="synthetic",
                allow_synthetic=is_synthetic_market_data_allowed(),
            )
            assert not valid
            assert "SYNTHETIC_DATA_PROHIBITED" in reason

    def test_synthetic_requires_explicit_opt_in_in_development(self):
        """Synthetic entry authorization must NOT be silently authorized in development without explicit opt-in."""
        with patch.dict(os.environ, {"ENVIRONMENT": "development", "NIVESH_ALLOW_SYNTHETIC_MARKET_DATA": "0"}):
            assert not is_synthetic_market_data_allowed()
        with patch.dict(os.environ, {"ENVIRONMENT": "development"}, clear=False):
            if "NIVESH_ALLOW_SYNTHETIC_MARKET_DATA" in os.environ:
                del os.environ["NIVESH_ALLOW_SYNTHETIC_MARKET_DATA"]
            assert not is_synthetic_market_data_allowed()

    def test_synthetic_explicit_opt_in_allowed_for_isolated_dev_tests(self):
        """Isolated tests can opt in explicitly via NIVESH_ALLOW_SYNTHETIC_MARKET_DATA=1 in development."""
        with patch.dict(os.environ, {"ENVIRONMENT": "development", "NIVESH_ALLOW_SYNTHETIC_MARKET_DATA": "1"}):
            assert is_synthetic_market_data_allowed()

    def test_synthetic_snapshot_metadata_tagged(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "development"}):
            data = market_snapshot()
            assert len(data) > 0
            for item in data:
                assert item["source"] == "SYNTHETIC_FIXTURE"
                assert item["data_provenance"] == "SYNTHETIC_FIXTURE"
                assert item["is_synthetic"] is True

            for idx in INDICES:
                assert idx["source"] == "SYNTHETIC_FIXTURE"
                assert idx["is_synthetic"] is True


class TestRiskEngineMarketDataReliability:
    """Verifies §4.E, Fix F-01, Fix F-03, Fix F-04: RiskEngine market data validation checks."""

    @pytest.fixture
    def mock_sqlite_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("CREATE TABLE risk_policies (user_id INT, min_confidence REAL, max_order_value REAL, max_gross_exposure_pct REAL, max_orders_per_day INT, max_open_positions INT, quote_max_age_seconds REAL, allow_derivatives INT)")
        cur.execute("CREATE TABLE settings (user_id INT, max_daily_loss_pct REAL, max_position_pct REAL)")
        cur.execute("CREATE TABLE portfolios (user_id INT, starting_capital REAL, cash REAL)")
        cur.execute("CREATE TABLE kill_switches (user_id INT, active INT, reason TEXT, activated_at TEXT, updated_at TEXT)")
        cur.execute("CREATE TABLE holdings (user_id INT, symbol TEXT, quantity INT, average_price REAL)")
        cur.execute("CREATE TABLE order_intents (user_id INT, created_at TEXT)")
        cur.execute("CREATE TABLE trades (user_id INT, realised_pnl REAL, created_at TEXT)")

        cur.execute("INSERT INTO risk_policies VALUES (1, 50.0, 500000.0, 100.0, 50, 10, 30.0, 1)")
        cur.execute("INSERT INTO settings VALUES (1, 5.0, 20.0)")
        cur.execute("INSERT INTO portfolios VALUES (1, 1000000.0, 1000000.0)")
        cur.execute("INSERT INTO kill_switches VALUES (1, 0, 'Inactive', '', '')")
        conn.commit()
        return conn

    def test_risk_engine_rejects_missing_quote_timestamp(self, mock_sqlite_db):
        """Fix F-01: Risk engine must reject orders when quote timestamp is missing."""
        engine = RiskEngine()
        order = {
            "symbol": "RELIANCE",
            "transaction_type": "BUY",
            "quantity": 1,
            "order_type": "LIMIT",
            "limit_price": 2990.0,
            "confidence": 75,
            "quote_source": "upstox_v3",
            # quote_timestamp omitted
        }
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test_db"}):
            with patch("backend.risk_engine.live_price_map", return_value={"RELIANCE": 2990.0}, create=True):
                decision = engine.evaluate(mock_sqlite_db, 1, order)
                assert not decision.allowed
                md_check = next(c for c in decision.checks if c["name"] == "market_data_reliability")
                assert not md_check["passed"]
                assert md_check["detail"] == "PRICE_OBSERVED_AT_TIMESTAMP_MISSING"

    def test_risk_engine_rejects_unreliable_market_data_price(self, mock_sqlite_db):
        engine = RiskEngine()
        order = {
            "symbol": "RELIANCE",
            "transaction_type": "BUY",
            "quantity": 10,
            "order_type": "LIMIT",
            "limit_price": 0.0,
            "confidence": 75,
            "quote_source": "upstox_v3",
            "quote_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        decision = engine.evaluate(mock_sqlite_db, 1, order)
        assert not decision.allowed
        md_check = next(c for c in decision.checks if c["name"] == "market_data_reliability")
        assert not md_check["passed"]

    def test_risk_engine_rejects_synthetic_data_in_production(self, mock_sqlite_db):
        """Fix F-04: Synthetic source is denied in production even if override is attempted."""
        engine = RiskEngine()
        order = {
            "symbol": "RELIANCE",
            "transaction_type": "BUY",
            "quantity": 1,
            "order_type": "LIMIT",
            "limit_price": 2990.0,
            "confidence": 75,
            "quote_source": "synthetic",
            "quote_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with patch.dict(os.environ, {
            "ENVIRONMENT": "production",
            "DATABASE_URL": "postgresql://test_db",
            "NIVESH_ALLOW_SYNTHETIC_MARKET_DATA": "1",
        }):
            with patch("backend.live_market_view.price_map", return_value={"RELIANCE": 2990.0}):
                decision = engine.evaluate(mock_sqlite_db, 1, order)
                assert not decision.allowed
                md_check = next(c for c in decision.checks if c["name"] == "market_data_reliability")
                assert not md_check["passed"]
                assert "SYNTHETIC_DATA_PROHIBITED" in md_check["detail"]

    def test_risk_engine_accepts_valid_fresh_live_quote(self, mock_sqlite_db):
        """Valid live quote with valid timestamp passes risk engine approval."""
        engine = RiskEngine()
        order = {
            "symbol": "RELIANCE",
            "transaction_type": "BUY",
            "quantity": 1,
            "order_type": "LIMIT",
            "limit_price": 2990.0,
            "confidence": 75,
            "quote_source": "upstox_v3",
            "quote_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with patch.dict(os.environ, {"ENVIRONMENT": "production", "DATABASE_URL": "postgresql://test_db"}):
            with patch("backend.live_market_view.price_map", return_value={"RELIANCE": 2990.0}):
                decision = engine.evaluate(mock_sqlite_db, 1, order)
                assert decision.allowed
                md_check = next(c for c in decision.checks if c["name"] == "market_data_reliability")
                assert md_check["passed"]

    def test_risk_engine_fails_closed_when_database_url_missing(self, mock_sqlite_db):
        """Fix F-03: Missing DATABASE_URL in production/staging must NOT fall back to synthetic prices."""
        engine = RiskEngine()
        order = {
            "symbol": "RELIANCE",
            "transaction_type": "BUY",
            "quantity": 1,
            "order_type": "LIMIT",
            "limit_price": 2990.0,
            "confidence": 75,
            "quote_source": "upstox_v3",
            "quote_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with patch.dict(os.environ, {
            "ENVIRONMENT": "production",
            "DATABASE_URL": "",
            "NIVESH_ALLOW_SYNTHETIC_MARKET_DATA": "1",
        }):
            decision = engine.evaluate(mock_sqlite_db, 1, order)
            assert not decision.allowed
            assert decision.reason == "MARKET_DATA_STORAGE_UNAVAILABLE"
            md_check = next(c for c in decision.checks if c["name"] == "market_data_reliability")
            assert not md_check["passed"]
            assert "MARKET_DATA_STORAGE_UNAVAILABLE" in md_check["detail"]


class TestOptionMarketDataValidation:
    """Verifies Fix F-02: Option contracts must be validated using their own market data."""

    def test_option_entry_rejected_when_option_quote_stale_despite_fresh_underlying(self):
        """Fix F-02: When underlying quote is fresh but option contract quote is stale, entry must be rejected."""
        now = datetime.now(timezone.utc)
        fresh_underlying_time = now - timedelta(seconds=5)  # 5s old (fresh)
        stale_option_time = now - timedelta(seconds=600)    # 10 minutes old (stale)

        # 1. Underlying quote validation passes
        underlying_valid, underlying_err = validate_market_data_for_entry(
            price=24800.0,
            observed_at=fresh_underlying_time,
            source="upstox_v3",
            max_age_seconds=30.0,
            reference_time=now,
        )
        assert underlying_valid
        assert underlying_err is None

        # 2. Option contract quote validation fails using its own timestamp
        option_valid, option_err = validate_market_data_for_entry(
            price=125.50,
            observed_at=stale_option_time,
            source="upstox_v3",
            max_age_seconds=30.0,
            reference_time=now,
        )
        assert not option_valid
        assert "PRICE_STALE" in option_err

    def test_option_entry_rejected_when_option_timestamp_missing(self):
        """Fix F-02: Option contract cannot fall back to underlying timestamp when its own timestamp is missing."""
        now = datetime.now(timezone.utc)
        # Option observed_at is None
        option_valid, option_err = validate_market_data_for_entry(
            price=125.50,
            observed_at=None,
            source="upstox_v3",
            max_age_seconds=30.0,
            reference_time=now,
        )
        assert not option_valid
        assert option_err == "PRICE_OBSERVED_AT_TIMESTAMP_MISSING"

    def test_nearest_option_target_extracts_bar_source_and_time(self):
        """Fix F-02: _nearest_option_target selects close_price, source, bar_time and returns them."""
        from backend.live_inference import LivePaperInference
        agent = LivePaperInference.__new__(LivePaperInference)
        agent.engine = MagicMock()
        agent.min_option_price = Decimal("5.0")
        agent.max_option_price = Decimal("500.0")

        mock_conn = MagicMock()
        agent.engine.connect.return_value.__enter__.return_value = mock_conn

        bar_time = datetime(2026, 10, 14, 10, 15, 0, tzinfo=timezone.utc)
        mock_row = {
            "instrument_id": 42,
            "instrument_token": 12345,
            "lot_size": 25,
            "instrument_type": "CE",
            "expiry": datetime(2026, 10, 20).date(),
            "strike": 24800.0,
            "close_price": 120.0,
            "source": "upstox_v3",
            "bar_time": bar_time,
        }
        mock_conn.execute.return_value.mappings.return_value.one_or_none.return_value = mock_row

        item = {
            "symbol": "NIFTY",
            "session": {"close": 24810.0, "timestamp": "2026-10-14T10:15:30Z"},
        }
        watermark = datetime(2026, 10, 14, 10, 15, 30, tzinfo=timezone.utc)
        target = agent._nearest_option_target(item, watermark, "CE")

        assert target is not None
        assert target["instrument_token"] == 12345
        assert target["source"] == "upstox_v3"
        assert target["observed_at"] == bar_time


class TestRiskEngineTimestampEnforcement:
    """Deterministic regression tests for Concern 1: Risk engine timestamp enforcement."""

    @pytest.fixture
    def mock_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("CREATE TABLE risk_policies (user_id INT, min_confidence REAL, max_order_value REAL, max_gross_exposure_pct REAL, max_orders_per_day INT, max_open_positions INT, quote_max_age_seconds REAL, allow_derivatives INT)")
        cur.execute("CREATE TABLE settings (user_id INT, max_daily_loss_pct REAL, max_position_pct REAL)")
        cur.execute("CREATE TABLE portfolios (user_id INT, starting_capital REAL, cash REAL)")
        cur.execute("CREATE TABLE kill_switches (user_id INT, active INT, reason TEXT, activated_at TEXT, updated_at TEXT)")
        cur.execute("CREATE TABLE holdings (user_id INT, symbol TEXT, quantity INT, average_price REAL)")
        cur.execute("CREATE TABLE order_intents (user_id INT, created_at TEXT)")
        cur.execute("CREATE TABLE trades (user_id INT, realised_pnl REAL, created_at TEXT)")

        cur.execute("INSERT INTO risk_policies VALUES (1, 50.0, 500000.0, 100.0, 50, 10, 30.0, 1)")
        cur.execute("INSERT INTO settings VALUES (1, 5.0, 20.0)")
        cur.execute("INSERT INTO portfolios VALUES (1, 1000000.0, 1000000.0)")
        cur.execute("INSERT INTO kill_switches VALUES (1, 0, 'Inactive', '', '')")
        conn.commit()
        return conn

    def test_risk_engine_rejects_blank_quote_timestamp(self, mock_db):
        """Blank string timestamp must fail closed."""
        engine = RiskEngine()
        order = {
            "symbol": "RELIANCE",
            "transaction_type": "BUY",
            "quantity": 1,
            "order_type": "LIMIT",
            "limit_price": 2990.0,
            "confidence": 75,
            "quote_source": "upstox_v3",
            "quote_timestamp": "   ",
        }
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test_db"}):
            with patch("backend.risk_engine.live_price_map", return_value={"RELIANCE": 2990.0}, create=True):
                decision = engine.evaluate(mock_db, 1, order)
                assert not decision.allowed
                md_check = next(c for c in decision.checks if c["name"] == "market_data_reliability")
                qf_check = next(c for c in decision.checks if c["name"] == "quote_freshness")
                lq_check = next(c for c in decision.checks if c["name"] == "live_quote_required")
                assert not md_check["passed"]
                assert not qf_check["passed"]
                assert not lq_check["passed"]
                assert md_check["detail"] == "PRICE_OBSERVED_AT_TIMESTAMP_MISSING"
                assert qf_check["detail"] == "PRICE_OBSERVED_AT_TIMESTAMP_MISSING"

    @pytest.mark.parametrize("malformed", ["not-a-date", "2026-99-99T99:99:99", "invalid"])
    def test_risk_engine_rejects_malformed_quote_timestamp(self, mock_db, malformed):
        """Malformed timestamps fail closed consistently across checks."""
        engine = RiskEngine()
        order = {
            "symbol": "RELIANCE",
            "transaction_type": "BUY",
            "quantity": 1,
            "order_type": "LIMIT",
            "limit_price": 2990.0,
            "confidence": 75,
            "quote_source": "upstox_v3",
            "quote_timestamp": malformed,
        }
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test_db"}):
            with patch("backend.risk_engine.live_price_map", return_value={"RELIANCE": 2990.0}, create=True):
                decision = engine.evaluate(mock_db, 1, order)
                assert not decision.allowed
                md_check = next(c for c in decision.checks if c["name"] == "market_data_reliability")
                qf_check = next(c for c in decision.checks if c["name"] == "quote_freshness")
                assert not md_check["passed"]
                assert not qf_check["passed"]
                assert "INVALID_OBSERVED_AT_TIMESTAMP" in md_check["detail"]
                assert "INVALID_OBSERVED_AT_TIMESTAMP" in qf_check["detail"]

    def test_risk_engine_rejects_stale_quote_timestamp(self, mock_db):
        """Stale quote (>30s) must fail closed consistently."""
        engine = RiskEngine()
        stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat()
        order = {
            "symbol": "RELIANCE",
            "transaction_type": "BUY",
            "quantity": 1,
            "order_type": "LIMIT",
            "limit_price": 2990.0,
            "confidence": 75,
            "quote_source": "upstox_v3",
            "quote_timestamp": stale_ts,
        }
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test_db"}):
            with patch("backend.risk_engine.live_price_map", return_value={"RELIANCE": 2990.0}, create=True):
                decision = engine.evaluate(mock_db, 1, order)
                assert not decision.allowed
                md_check = next(c for c in decision.checks if c["name"] == "market_data_reliability")
                qf_check = next(c for c in decision.checks if c["name"] == "quote_freshness")
                assert not md_check["passed"]
                assert not qf_check["passed"]
                assert "PRICE_STALE" in md_check["detail"]
                assert "PRICE_STALE" in qf_check["detail"]

    def test_risk_engine_rejects_future_dated_quote_timestamp(self, mock_db):
        """Excessively future-dated quote must fail closed consistently."""
        engine = RiskEngine()
        future_ts = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        order = {
            "symbol": "RELIANCE",
            "transaction_type": "BUY",
            "quantity": 1,
            "order_type": "LIMIT",
            "limit_price": 2990.0,
            "confidence": 75,
            "quote_source": "upstox_v3",
            "quote_timestamp": future_ts,
        }
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test_db"}):
            with patch("backend.risk_engine.live_price_map", return_value={"RELIANCE": 2990.0}, create=True):
                decision = engine.evaluate(mock_db, 1, order)
                assert not decision.allowed
                md_check = next(c for c in decision.checks if c["name"] == "market_data_reliability")
                qf_check = next(c for c in decision.checks if c["name"] == "quote_freshness")
                assert not md_check["passed"]
                assert not qf_check["passed"]
                assert "PRICE_TIMESTAMP_IN_FUTURE" in md_check["detail"]
                assert "PRICE_TIMESTAMP_IN_FUTURE" in qf_check["detail"]

    def test_quote_freshness_cannot_independently_pass_when_timestamp_absent(self, mock_db):
        """Quote freshness check CANNOT pass if timestamp is missing or empty."""
        engine = RiskEngine()
        order = {
            "symbol": "RELIANCE",
            "transaction_type": "BUY",
            "quantity": 1,
            "order_type": "LIMIT",
            "limit_price": 2990.0,
            "confidence": 75,
            "quote_source": "upstox_v3",
            # No quote_timestamp
        }
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test_db"}):
            with patch("backend.risk_engine.live_price_map", return_value={"RELIANCE": 2990.0}, create=True):
                decision = engine.evaluate(mock_db, 1, order)
                qf_check = next(c for c in decision.checks if c["name"] == "quote_freshness")
                assert not qf_check["passed"]


class TestPrimaryEntryMetadataEnforcement:
    """Deterministic regression tests for Concern 2: Primary entry metadata."""

    def test_primary_entry_rejects_missing_source_metadata(self):
        """validate_market_data_for_entry requires non-empty source metadata."""
        valid, err = validate_market_data_for_entry(
            price=2990.0,
            observed_at=datetime.now(timezone.utc),
            source=None,
        )
        assert not valid
        assert err == "PRICE_SOURCE_METADATA_MISSING"

        valid_empty, err_empty = validate_market_data_for_entry(
            price=2990.0,
            observed_at=datetime.now(timezone.utc),
            source="   ",
        )
        assert not valid_empty
        assert err_empty == "PRICE_SOURCE_METADATA_MISSING"

    def test_primary_entry_rejects_target_missing_observed_at_without_fallback(self):
        """Primary entry validator does NOT fall back to session timestamp when target lacks observed_at."""
        target = {
            "price": 2990.0,
            "source": "upstox_v3",
            "observed_at": None,  # target missing timestamp
        }
        valid, err = validate_market_data_for_entry(
            price=target.get("price"),
            observed_at=target.get("observed_at"),
            source=target.get("source"),
            max_age_seconds=30.0,
            allow_synthetic=False,
        )
        assert not valid
        assert err == "PRICE_OBSERVED_AT_TIMESTAMP_MISSING"

    def test_primary_entry_rejects_target_missing_source_without_fallback(self):
        """Primary entry validator does NOT fall back to session source when target lacks source."""
        target = {
            "price": 2990.0,
            "source": None,  # target missing source
            "observed_at": datetime.now(timezone.utc),
        }
        valid, err = validate_market_data_for_entry(
            price=target.get("price"),
            observed_at=target.get("observed_at"),
            source=target.get("source"),
            max_age_seconds=30.0,
            allow_synthetic=False,
        )
        assert not valid
        assert err == "PRICE_SOURCE_METADATA_MISSING"


class TestExplicitProvenancePolicy:
    """Deterministic regression tests for Concern 4: Explicit provenance policy."""

    def test_entry_eligible_provenance_is_strictly_live_stream(self):
        """Only LIVE_STREAM is eligible for live entries."""
        assert ENTRY_ELIGIBLE_PROVENANCES == frozenset({DataProvenance.LIVE_STREAM})

    def test_cached_provenance_rejected_even_when_fresh(self):
        now = datetime.now(timezone.utc)
        valid, err = validate_market_data_for_entry(
            price=2990.0,
            observed_at=now,
            source="redis_tick",
            max_age_seconds=30.0,
            reference_time=now,
        )
        assert not valid
        assert "CACHED_DATA_CANNOT_AUTHORIZE_ENTRY" in err

    def test_rest_backfill_provenance_rejected_even_when_fresh(self):
        now = datetime.now(timezone.utc)
        valid, err = validate_market_data_for_entry(
            price=2990.0,
            observed_at=now,
            source="upstox_rest_5m",
            max_age_seconds=30.0,
            reference_time=now,
        )
        assert not valid
        assert "REST_BACKFILL_CANNOT_AUTHORIZE_ENTRY" in err

    def test_historical_bhavcopy_rejected_even_when_fresh(self):
        now = datetime.now(timezone.utc)
        valid, err = validate_market_data_for_entry(
            price=2990.0,
            observed_at=now,
            source="official_exchange_bhavcopy",
            max_age_seconds=30.0,
            reference_time=now,
        )
        assert not valid
        assert "HISTORICAL_BHAVCOPY_CANNOT_MASQUERADE_AS_LIVE_ENTRY" in err

    def test_analytical_source_classified_as_synthetic_and_prohibited_for_entry(self):
        now = datetime.now(timezone.utc)
        assert classify_provenance("analytical_black_scholes") == DataProvenance.SYNTHETIC_FIXTURE
        valid, err = validate_market_data_for_entry(
            price=125.0,
            observed_at=now,
            source="analytical_black_scholes",
            max_age_seconds=30.0,
            allow_synthetic=False,
            reference_time=now,
        )
        assert not valid
        assert "SYNTHETIC_DATA_PROHIBITED_FOR_ENTRY" in err

    def test_unknown_source_classified_as_synthetic_and_prohibited_for_entry(self):
        now = datetime.now(timezone.utc)
        assert classify_provenance("some_random_vendor") == DataProvenance.SYNTHETIC_FIXTURE
        valid, err = validate_market_data_for_entry(
            price=2990.0,
            observed_at=now,
            source="some_random_vendor",
            max_age_seconds=30.0,
            allow_synthetic=False,
            reference_time=now,
        )
        assert not valid
        assert "SYNTHETIC_DATA_PROHIBITED_FOR_ENTRY" in err


class TestSymbolSpecificRiskPricing:
    """Deterministic regression tests for Concern 5: Symbol-specific risk pricing."""

    @pytest.fixture
    def mock_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("CREATE TABLE risk_policies (user_id INT, min_confidence REAL, max_order_value REAL, max_gross_exposure_pct REAL, max_orders_per_day INT, max_open_positions INT, quote_max_age_seconds REAL, allow_derivatives INT)")
        cur.execute("CREATE TABLE settings (user_id INT, max_daily_loss_pct REAL, max_position_pct REAL)")
        cur.execute("CREATE TABLE portfolios (user_id INT, starting_capital REAL, cash REAL)")
        cur.execute("CREATE TABLE kill_switches (user_id INT, active INT, reason TEXT, activated_at TEXT, updated_at TEXT)")
        cur.execute("CREATE TABLE holdings (user_id INT, symbol TEXT, quantity INT, average_price REAL)")
        cur.execute("CREATE TABLE order_intents (user_id INT, created_at TEXT)")
        cur.execute("CREATE TABLE trades (user_id INT, realised_pnl REAL, created_at TEXT)")

        cur.execute("INSERT INTO risk_policies VALUES (1, 50.0, 500000.0, 100.0, 50, 10, 30.0, 1)")
        cur.execute("INSERT INTO settings VALUES (1, 5.0, 20.0)")
        cur.execute("INSERT INTO portfolios VALUES (1, 1000000.0, 1000000.0)")
        cur.execute("INSERT INTO kill_switches VALUES (1, 0, 'Inactive', '', '')")
        conn.commit()
        return conn

    def test_risk_engine_rejects_missing_order_symbol_in_price_map(self, mock_db):
        """Risk engine fails closed when the exact symbol is absent from the price map."""
        engine = RiskEngine()
        order = {
            "symbol": "TCS",  # TCS is absent from live price map
            "transaction_type": "BUY",
            "quantity": 1,
            "order_type": "LIMIT",
            "limit_price": 3800.0,
            "confidence": 75,
            "quote_source": "upstox_v3",
            "quote_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test_db"}):
            with patch("backend.risk_engine.live_price_map", return_value={"RELIANCE": 2990.0, "INFY": 1500.0}, create=True):
                decision = engine.evaluate(mock_db, 1, order)
                assert not decision.allowed
                assert "SYMBOL_PRICE_UNAVAILABLE: TCS" in decision.reason
                md_check = next(c for c in decision.checks if c["name"] == "market_data_reliability")
                assert not md_check["passed"]
                assert "SYMBOL_PRICE_UNAVAILABLE" in md_check["detail"]

    @pytest.mark.parametrize("bad_price", [0.0, -10.0, float("nan"), float("inf"), float("-inf")])
    def test_risk_engine_rejects_invalid_order_symbol_price(self, mock_db, bad_price):
        """Risk engine fails closed when the exact symbol has an invalid price."""
        engine = RiskEngine()
        order = {
            "symbol": "RELIANCE",
            "transaction_type": "BUY",
            "quantity": 1,
            "order_type": "LIMIT",
            "limit_price": 2990.0,
            "confidence": 75,
            "quote_source": "upstox_v3",
            "quote_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test_db"}):
            with patch("backend.risk_engine.live_price_map", return_value={"RELIANCE": bad_price}, create=True):
                decision = engine.evaluate(mock_db, 1, order)
                assert not decision.allowed
                assert "SYMBOL_PRICE_INVALID: RELIANCE" in decision.reason

    def test_risk_engine_derivative_resolves_underlying_when_contract_absent(self, mock_db):
        """Derivative order routes to underlying symbol price if contract is not directly in price map."""
        engine = RiskEngine()
        order = {
            "symbol": "NIFTY24OCT25000CE",
            "exchange": "NFO",
            "underlying": "NIFTY",
            "transaction_type": "BUY",
            "quantity": 50,
            "order_type": "LIMIT",
            "limit_price": 125.0,
            "confidence": 75,
            "quote_source": "upstox_v3",
            "quote_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test_db"}):
            with patch("backend.risk_engine.live_price_map", return_value={"NIFTY": 24800.0}, create=True):
                decision = engine.evaluate(mock_db, 1, order)
                assert decision.allowed

    def test_risk_engine_rejects_stale_or_unreliable_symbol_price_dict(self, mock_db):
        """When prices contains dictionary metadata, stale quotes fail closed with SYMBOL_PRICE_UNRELIABLE."""
        engine = RiskEngine()
        order = {
            "symbol": "RELIANCE",
            "transaction_type": "BUY",
            "quantity": 1,
            "order_type": "LIMIT",
            "limit_price": 2990.0,
            "confidence": 75,
            "quote_source": "upstox_v3",
            "quote_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test_db"}):
            with patch("backend.risk_engine.live_price_map", return_value={
                "RELIANCE": {
                    "price": 2990.0,
                    "source": "upstox_v3",
                    "observed_at": stale_ts,
                }
            }, create=True):
                decision = engine.evaluate(mock_db, 1, order)
                assert not decision.allowed
                assert "SYMBOL_PRICE_UNRELIABLE: RELIANCE" in decision.reason

    def test_risk_engine_rejects_quote_symbol_mismatch(self, mock_db):
        """Risk engine fails closed when quote_symbol does not match order symbol."""
        engine = RiskEngine()
        order = {
            "symbol": "RELIANCE",
            "quote_symbol": "INFY",  # Quote belongs to a different instrument!
            "transaction_type": "BUY",
            "quantity": 1,
            "order_type": "LIMIT",
            "limit_price": 2990.0,
            "confidence": 75,
            "quote_source": "upstox_v3",
            "quote_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test_db"}):
            with patch("backend.risk_engine.live_price_map", return_value={"RELIANCE": 2990.0, "INFY": 1500.0}, create=True):
                decision = engine.evaluate(mock_db, 1, order)
                assert not decision.allowed
                assert "QUOTE_SYMBOL_MISMATCH: INFY != RELIANCE" in decision.reason
                md_check = next(c for c in decision.checks if c["name"] == "market_data_reliability")
                assert not md_check["passed"]
                assert "QUOTE_SYMBOL_MISMATCH" in md_check["detail"]


class TestSafetyFusesPreserved:
    """Verifies that live execution fuses remain strictly locked."""

    def test_execution_fuses_remain_locked(self):
        assert not LIVE_TRADING_ENABLED
        assert os.getenv("NIVESH_LIVE_TRADING_ENABLED", "0") != "1"
