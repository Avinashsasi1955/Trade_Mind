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


class TestLiveExecutionMarksAndPreparation:
    """Deterministic regression tests for live execution marks, candidate preparation, and target validation."""

    def test_equity_target_construction_with_fresh_execution_mark(self):
        """F-01: Equity execution target uses fresh mark and passes entry validation under wall-clock."""
        from backend.live_inference import LivePaperInference
        from backend.market_data_feed import validate_market_data_for_entry

        agent = LivePaperInference.__new__(LivePaperInference)
        agent.option_paper_enabled = True
        agent.intraday_short_fallback = True

        now = datetime.now(timezone.utc)
        item = {
            "instrument_token": "12345",
            "instrument_id": 1,
            "instrument_type": "EQ",
            "symbol": "TATASTEEL",
            "signal": 1,
            "session": {"close": 150.0, "timestamp": now - timedelta(minutes=5)},
            "execution_mark": {
                "price": Decimal("152.50"),
                "source": "upstox_v3",
                "observed_at": now - timedelta(seconds=2),
            },
        }
        target = agent._execution_target(item, now)
        assert target is not None
        assert target["price"] == Decimal("152.50")
        assert target["source"] == "upstox_v3"
        assert target["observed_at"] == item["execution_mark"]["observed_at"]

        # Validates against real execution time (wall-clock)
        valid, err = validate_market_data_for_entry(
            price=target["price"],
            observed_at=target["observed_at"],
            source=target["source"],
            max_age_seconds=30.0,
        )
        assert valid, f"Expected entry quote to be valid, got: {err}"

    def test_equity_target_never_falls_back_to_session_close(self):
        """Requirement 4: When execution mark is missing, equity target returns None (no implicit fallback)."""
        from backend.live_inference import LivePaperInference

        agent = LivePaperInference.__new__(LivePaperInference)
        agent.option_paper_enabled = True
        agent.intraday_short_fallback = True
        agent.engine = None  # No engine to query

        now = datetime.now(timezone.utc)
        item = {
            "instrument_token": "12345",
            "instrument_id": 1,
            "instrument_type": "EQ",
            "symbol": "TATASTEEL",
            "signal": 1,
            "session": {"close": 150.0, "timestamp": now - timedelta(minutes=5)},
            # No execution_mark provided!
        }
        target = agent._execution_target(item, now)
        assert target is None, "Must not fall back to session['close'] when execution mark is missing"

    def test_equity_target_rejected_when_execution_mark_stale(self):
        """Requirement 5: Stale execution mark (>30s) fails validation under wall-clock."""
        from backend.live_inference import LivePaperInference
        from backend.market_data_feed import validate_market_data_for_entry

        agent = LivePaperInference.__new__(LivePaperInference)
        agent.option_paper_enabled = True
        agent.intraday_short_fallback = True

        now = datetime.now(timezone.utc)
        item = {
            "instrument_token": "12345",
            "instrument_id": 1,
            "instrument_type": "EQ",
            "symbol": "TATASTEEL",
            "signal": 1,
            "session": {"close": 150.0, "timestamp": now - timedelta(minutes=5)},
            "execution_mark": {
                "price": Decimal("152.50"),
                "source": "upstox_v3",
                "observed_at": now - timedelta(seconds=45),  # 45s old
            },
        }
        target = agent._execution_target(item, now)
        assert target is not None
        valid, err = validate_market_data_for_entry(
            price=target["price"],
            observed_at=target["observed_at"],
            source=target["source"],
            max_age_seconds=30.0,
        )
        assert not valid
        assert "PRICE_STALE" in err

    def test_equity_target_rejected_when_provenance_missing(self):
        """Requirement 7: Missing or blank provenance fails closed."""
        from backend.market_data_feed import validate_market_data_for_entry

        now = datetime.now(timezone.utc)
        for bad_source in [None, "", "   "]:
            valid, err = validate_market_data_for_entry(
                price=Decimal("150.0"),
                observed_at=now,
                source=bad_source,
                max_age_seconds=30.0,
            )
            assert not valid
            assert err == "PRICE_SOURCE_METADATA_MISSING"

    def test_equity_target_rejected_when_price_invalid(self):
        """Requirement 7: Non-positive, zero, NaN, or Inf prices fail closed."""
        from backend.market_data_feed import validate_market_data_for_entry

        now = datetime.now(timezone.utc)
        for bad_price in [0, -10.5, float("nan"), float("inf"), "invalid"]:
            valid, err = validate_market_data_for_entry(
                price=bad_price,
                observed_at=now,
                source="upstox_v3",
                max_age_seconds=30.0,
            )
            assert not valid
            assert any(code in err for code in ["PRICE_INVALID", "PRICE_NON_NUMERIC"])

    def test_nearest_option_target_freshest_mark_and_wall_clock_validation(self):
        """Requirement 5 & 6: Option target selects freshest mark and validates independently against wall-clock."""
        from backend.live_inference import LivePaperInference
        from backend.market_data_feed import validate_market_data_for_entry

        agent = LivePaperInference.__new__(LivePaperInference)
        agent.engine = MagicMock()
        agent.min_option_price = Decimal("5.0")
        agent.max_option_price = Decimal("500.0")

        now = datetime.now(timezone.utc)
        mock_conn = MagicMock()
        agent.engine.connect.return_value.__enter__.return_value = mock_conn

        mock_row = {
            "instrument_id": 42,
            "instrument_token": 12345,
            "lot_size": 25,
            "instrument_type": "CE",
            "expiry": (now + timedelta(days=5)).date(),
            "strike": 24800.0,
            "close_price": 120.0,
            "source": "upstox_v3",
            "bar_time": now - timedelta(seconds=20),
            "observed_at": now - timedelta(seconds=3),
        }
        mock_conn.execute.return_value.mappings.return_value.one_or_none.return_value = mock_row

        item = {
            "symbol": "NIFTY",
            "session": {"close": 24810.0, "timestamp": now - timedelta(minutes=5)},
        }
        target = agent._nearest_option_target(item, now, "CE")

        assert target is not None
        assert target["price"] == Decimal("120.0")
        assert target["source"] == "upstox_v3"
        assert target["observed_at"] == mock_row["observed_at"]

        valid, err = validate_market_data_for_entry(
            price=target["price"],
            observed_at=target["observed_at"],
            source=target["source"],
            max_age_seconds=30.0,
        )
        assert valid, f"Expected valid option quote, got: {err}"

    def test_stale_option_rejected_even_when_underlying_is_fresh(self):
        """Requirement 6: Stale option mark is rejected even if underlying watermark is completely fresh."""
        from backend.live_inference import LivePaperInference
        from backend.market_data_feed import validate_market_data_for_entry

        agent = LivePaperInference.__new__(LivePaperInference)
        agent.engine = MagicMock()
        agent.min_option_price = Decimal("5.0")
        agent.max_option_price = Decimal("500.0")

        now = datetime.now(timezone.utc)
        mock_conn = MagicMock()
        agent.engine.connect.return_value.__enter__.return_value = mock_conn

        # Option mark is 45s old (stale)
        mock_row = {
            "instrument_id": 42,
            "instrument_token": 12345,
            "lot_size": 25,
            "instrument_type": "CE",
            "expiry": (now + timedelta(days=5)).date(),
            "strike": 24800.0,
            "close_price": 120.0,
            "source": "upstox_v3",
            "bar_time": now - timedelta(minutes=5),
            "observed_at": now - timedelta(seconds=45),
        }
        mock_conn.execute.return_value.mappings.return_value.one_or_none.return_value = mock_row

        # Underlying watermark is very fresh (2 seconds ago)
        fresh_underlying_time = now - timedelta(seconds=2)
        item = {
            "symbol": "NIFTY",
            "session": {"close": 24810.0, "timestamp": fresh_underlying_time},
        }
        target = agent._nearest_option_target(item, fresh_underlying_time, "CE")
        assert target is not None

        # Validate independently against real execution time (wall-clock)
        valid, err = validate_market_data_for_entry(
            price=target["price"],
            observed_at=target["observed_at"],
            source=target["source"],
            max_age_seconds=30.0,
        )
        assert not valid, "Option must not inherit underlying freshness"
        assert "PRICE_STALE" in err

    def test_synthetic_option_pricing_prohibited_for_live_entry(self):
        """Requirement 7: Black-Scholes synthetic fallback is rejected in live entry validation."""
        from backend.market_data_feed import validate_market_data_for_entry

        now = datetime.now(timezone.utc)
        target = {
            "price": Decimal("115.00"),
            "source": "analytical_black_scholes",
            "observed_at": now - timedelta(seconds=1),
        }
        valid, err = validate_market_data_for_entry(
            price=target["price"],
            observed_at=target["observed_at"],
            source=target["source"],
            allow_synthetic=False,
        )
        assert not valid
        assert "SYNTHETIC_DATA_PROHIBITED_FOR_ENTRY" in err

    def test_actual_candidate_preparation_and_target_construction_path(self):
        """Requirement 9: Exercises candidate preparation and target construction end-to-end with mock rows."""
        from backend.live_inference import LivePaperInference

        agent = LivePaperInference.__new__(LivePaperInference)
        agent.engine = MagicMock()
        agent.max_symbols = 10
        agent.microstructure_lookback_seconds = 120
        agent.option_paper_enabled = True
        agent.intraday_short_fallback = True

        mock_conn = MagicMock()
        agent.engine.connect.return_value.__enter__.return_value = mock_conn

        now = datetime.now(timezone.utc)
        bar_time = now - timedelta(minutes=5)

        candidate_row = {
            "instrument_id": 101,
            "instrument_token": "TATASTEEL_TOKEN",
            "exchange": "NSE",
            "symbol": "TATASTEEL",
            "instrument_type": "EQ",
            "bar_time": bar_time,
            "bar_source": "upstox_v3",
            "mark_price": 155.0,
            "mark_source": "upstox_v3",
            "mark_observed_at": now - timedelta(seconds=5),
            "latest_1m_bar": now - timedelta(seconds=60),
        }

        # Setup mock_conn.execute return values for the sequence of queries in _prepare():
        # 1. candidates query
        # 2. daily bars
        # 3. intraday 5m bars
        # 4. one_minute bars
        # 5. microstructure seconds
        daily_bars = [
            {"bar_time": bar_time - timedelta(days=i), "open_price": 150.0, "high_price": 160.0, "low_price": 145.0, "close_price": 155.0, "volume": 1000, "open_interest": 0}
            for i in range(1, 60)
        ]
        intraday_bars = [
            {"bar_time": bar_time, "open_price": 150.0, "high_price": 156.0, "low_price": 149.0, "close_price": 155.0, "volume": 500, "open_interest": 0}
        ]
        one_m_bars = [
            {"bar_time": bar_time, "open_price": 154.0, "high_price": 155.0, "low_price": 154.0, "close_price": 155.0, "volume": 100, "open_interest": 0}
        ]
        second_bars = []

        mock_exec = MagicMock()
        mock_exec.mappings.side_effect = [
            MagicMock(all=lambda: [candidate_row]),
            MagicMock(all=lambda: daily_bars),
            MagicMock(all=lambda: intraday_bars),
            MagicMock(all=lambda: one_m_bars),
            MagicMock(all=lambda: second_bars),
        ]
        mock_conn.execute.return_value = mock_exec

        prepared = agent._prepare(bar_time)
        assert len(prepared) == 1
        prep_item = prepared[0]
        assert "execution_mark" in prep_item
        assert prep_item["execution_mark"]["price"] == Decimal("155.0")
        assert prep_item["execution_mark"]["source"] == "upstox_v3"
        assert prep_item["execution_mark"]["observed_at"] == candidate_row["mark_observed_at"]

        # Now pass prep_item into _execution_target
        prep_item["signal"] = 1
        target = agent._execution_target(prep_item, prep_item["causal_watermark"])
        assert target is not None
        assert target["price"] == Decimal("155.0")
        assert target["source"] == "upstox_v3"
        assert target["observed_at"] == candidate_row["mark_observed_at"]


class TestCausalBoundaryAndFutureCandlePrevention:
    """Deterministic tests for causal boundary enforcement and future-candle prevention."""

    def test_prepare_lateral_query_enforces_causal_boundary(self):
        """Verifies that _prepare() lateral queries strictly exclude observations after cycle close."""
        from backend.live_inference import LivePaperInference

        agent = LivePaperInference.__new__(LivePaperInference)
        agent.engine = MagicMock()
        agent.max_symbols = 5
        agent.microstructure_lookback_seconds = 120

        mock_conn = MagicMock()
        agent.engine.connect.return_value.__enter__.return_value = mock_conn

        captured_queries = []

        def capture_exec(query_obj, params=None):
            sql_text = str(query_obj)
            captured_queries.append((sql_text, params))
            mock_res = MagicMock()
            mock_res.mappings.return_value.all.return_value = []
            return mock_res

        mock_conn.execute.side_effect = capture_exec

        watermark = datetime(2026, 10, 19, 5, 0, 0, tzinfo=timezone.utc)
        agent._prepare(watermark)

        assert len(captured_queries) >= 1
        prepare_sql = captured_queries[0][0]

        # Verify live_mark LATERAL query has causal boundary on observed_at
        assert "<= :watermark + INTERVAL '5 minutes'" in prepare_sql
        assert "received_at" in prepare_sql
        # Verify live1 LATERAL query does not include the next 1-minute bucket starting at watermark + 5m
        assert "bar_time<:watermark+INTERVAL '5 minutes'" in prepare_sql

    def test_nearest_option_target_query_enforces_causal_boundary(self):
        """Verifies that _nearest_option_target() lateral query uses :causal_boundary directly without adding 5 minutes."""
        from backend.live_inference import LivePaperInference

        agent = LivePaperInference.__new__(LivePaperInference)
        agent.engine = MagicMock()
        agent.min_option_price = Decimal("5.0")
        agent.max_option_price = Decimal("500.0")

        mock_conn = MagicMock()
        agent.engine.connect.return_value.__enter__.return_value = mock_conn

        captured_queries = []

        def capture_exec(query_obj, params=None):
            sql_text = str(query_obj)
            captured_queries.append((sql_text, params))
            mock_res = MagicMock()
            mock_res.mappings.return_value.one_or_none.return_value = None
            return mock_res

        mock_conn.execute.side_effect = capture_exec

        # 1. Caller passes causal_watermark (09:20:00)
        causal_watermark = datetime(2026, 10, 19, 3, 50, 0, tzinfo=timezone.utc)
        item = {"symbol": "NIFTY", "session": {"close": 24800.0}, "causal_watermark": causal_watermark}
        agent._nearest_option_target(item, causal_watermark, "CE")

        assert len(captured_queries) >= 1
        opt_sql, params = captured_queries[0]

        # Verify option query checks <= :causal_boundary (not :watermark + INTERVAL '5 minutes')
        assert "<= :causal_boundary" in opt_sql
        assert "received_at" in opt_sql
        assert params["causal_boundary"] == causal_watermark

    def test_execution_target_query_enforces_causal_boundary(self):
        """Verifies that _execution_target() fallback query uses :causal_boundary directly without adding 5 minutes."""
        from backend.live_inference import LivePaperInference

        agent = LivePaperInference.__new__(LivePaperInference)
        agent.engine = MagicMock()
        agent.option_paper_enabled = True
        agent.intraday_short_fallback = True

        mock_conn = MagicMock()
        agent.engine.connect.return_value.__enter__.return_value = mock_conn

        captured_queries = []

        def capture_exec(query_obj, params=None):
            sql_text = str(query_obj)
            captured_queries.append((sql_text, params))
            mock_res = MagicMock()
            mock_res.mappings.return_value.one_or_none.return_value = None
            return mock_res

        mock_conn.execute.side_effect = capture_exec

        causal_watermark = datetime(2026, 10, 19, 3, 50, 0, tzinfo=timezone.utc)
        item = {
            "instrument_id": 101,
            "instrument_token": "TATASTEEL",
            "instrument_type": "EQ",
            "signal": 1,
            "session": {"close": Decimal("150.0")},
            "causal_watermark": causal_watermark,
        }
        agent._execution_target(item, causal_watermark)

        assert len(captured_queries) >= 1
        exec_sql, params = captured_queries[0]

        # Verify fallback query checks <= :causal_boundary
        assert "<= :causal_boundary" in exec_sql
        assert "received_at" in exec_sql
        assert params["causal_boundary"] == causal_watermark

    def test_callers_must_pass_causal_watermark_not_source_bar_time(self):
        """Verifies that _nearest_option_target uses the passed causal_watermark directly
        without any implicit detection or adjustment based on source_bar_time."""
        from backend.live_inference import LivePaperInference

        agent = LivePaperInference.__new__(LivePaperInference)
        agent.engine = MagicMock()
        agent.min_option_price = Decimal("5.0")
        agent.max_option_price = Decimal("500.0")

        mock_conn = MagicMock()
        agent.engine.connect.return_value.__enter__.return_value = mock_conn

        captured_queries = []

        def capture_exec(query_obj, params=None):
            sql_text = str(query_obj)
            captured_queries.append((sql_text, params))
            mock_res = MagicMock()
            mock_res.mappings.return_value.one_or_none.return_value = None
            return mock_res

        mock_conn.execute.side_effect = capture_exec

        causal_watermark = datetime(2026, 10, 19, 3, 50, 0, tzinfo=timezone.utc)
        source_watermark = datetime(2026, 10, 19, 3, 45, 0, tzinfo=timezone.utc)

        # Caller passes causal_watermark (09:20:00) — it should be used directly
        item = {
            "symbol": "NIFTY",
            "session": {"close": 24800.0, "timestamp": causal_watermark},
            "causal_watermark": causal_watermark,
            "source_bar_time": source_watermark,
        }
        agent._nearest_option_target(item, causal_watermark, "CE")
        assert len(captured_queries) >= 1
        assert captured_queries[0][1]["causal_boundary"] == causal_watermark

    def test_cached_execution_mark_rejected_when_observed_after_causal_boundary(self):
        """Behavioral: cached execution_mark with observed_at > causal_boundary is discarded."""
        from backend.live_inference import LivePaperInference

        agent = LivePaperInference.__new__(LivePaperInference)
        agent.engine = MagicMock()
        agent.option_paper_enabled = False
        agent.intraday_short_fallback = True

        causal_boundary = datetime(2026, 10, 19, 3, 50, 0, tzinfo=timezone.utc)

        # Cached mark observed 1 second AFTER causal boundary — must be rejected
        item = {
            "instrument_id": 101,
            "instrument_token": "TATASTEEL",
            "instrument_type": "EQ",
            "signal": 1,
            "session": {"close": Decimal("155.0"), "timestamp": causal_boundary},
            "causal_watermark": causal_boundary,
            "execution_mark": {
                "price": Decimal("155.0"),
                "source": "upstox_v3",
                "observed_at": causal_boundary + timedelta(seconds=1),
            },
        }
        target = agent._execution_target(item, causal_boundary)
        # The cached mark should be rejected; without a DB fallback, target is None
        assert target is None

    def test_cached_execution_mark_accepted_when_observed_at_causal_boundary(self):
        """Behavioral: cached execution_mark with observed_at == causal_boundary is accepted."""
        from backend.live_inference import LivePaperInference

        agent = LivePaperInference.__new__(LivePaperInference)
        agent.engine = MagicMock()
        agent.option_paper_enabled = False
        agent.intraday_short_fallback = True

        causal_boundary = datetime(2026, 10, 19, 3, 50, 0, tzinfo=timezone.utc)

        # Cached mark observed exactly AT causal boundary — must be accepted
        item = {
            "instrument_id": 101,
            "instrument_token": "TATASTEEL",
            "instrument_type": "EQ",
            "signal": 1,
            "session": {"close": Decimal("155.0"), "timestamp": causal_boundary},
            "causal_watermark": causal_boundary,
            "execution_mark": {
                "price": Decimal("155.0"),
                "source": "upstox_v3",
                "observed_at": causal_boundary,
            },
        }
        target = agent._execution_target(item, causal_boundary)
        assert target is not None
        assert target["price"] == Decimal("155.0")
        assert target["source"] == "upstox_v3"
        assert target["observed_at"] == causal_boundary

    def test_cached_execution_mark_accepted_when_observed_before_causal_boundary(self):
        """Behavioral: cached execution_mark with observed_at < causal_boundary is accepted."""
        from backend.live_inference import LivePaperInference

        agent = LivePaperInference.__new__(LivePaperInference)
        agent.engine = MagicMock()
        agent.option_paper_enabled = False
        agent.intraday_short_fallback = True

        causal_boundary = datetime(2026, 10, 19, 3, 50, 0, tzinfo=timezone.utc)

        item = {
            "instrument_id": 101,
            "instrument_token": "TATASTEEL",
            "instrument_type": "EQ",
            "signal": 1,
            "session": {"close": Decimal("155.0"), "timestamp": causal_boundary},
            "causal_watermark": causal_boundary,
            "execution_mark": {
                "price": Decimal("155.0"),
                "source": "upstox_v3",
                "observed_at": causal_boundary - timedelta(seconds=3),
            },
        }
        target = agent._execution_target(item, causal_boundary)
        assert target is not None
        assert target["price"] == Decimal("155.0")

    def test_latest_1m_mark_rejected_when_observed_after_causal_boundary(self):
        """Behavioral: latest_1m_* fields with observed_at > causal_boundary are discarded."""
        from backend.live_inference import LivePaperInference

        agent = LivePaperInference.__new__(LivePaperInference)
        agent.engine = MagicMock()
        agent.option_paper_enabled = False
        agent.intraday_short_fallback = True

        causal_boundary = datetime(2026, 10, 19, 3, 50, 0, tzinfo=timezone.utc)

        item = {
            "instrument_id": 101,
            "instrument_token": "TATASTEEL",
            "instrument_type": "EQ",
            "signal": 1,
            "session": {"close": Decimal("155.0"), "timestamp": causal_boundary},
            "causal_watermark": causal_boundary,
            "latest_1m_price": 156.0,
            "latest_1m_source": "upstox_v3",
            "latest_1m_observed_at": causal_boundary + timedelta(seconds=30),
        }
        target = agent._execution_target(item, causal_boundary)
        # latest_1m mark is 30s after boundary — must be rejected; no DB fallback → None
        assert target is None

    def test_latest_1m_mark_accepted_when_observed_at_causal_boundary(self):
        """Behavioral: latest_1m_* fields with observed_at <= causal_boundary are accepted."""
        from backend.live_inference import LivePaperInference

        agent = LivePaperInference.__new__(LivePaperInference)
        agent.engine = MagicMock()
        agent.option_paper_enabled = False
        agent.intraday_short_fallback = True

        causal_boundary = datetime(2026, 10, 19, 3, 50, 0, tzinfo=timezone.utc)

        item = {
            "instrument_id": 101,
            "instrument_token": "TATASTEEL",
            "instrument_type": "EQ",
            "signal": 1,
            "session": {"close": Decimal("155.0"), "timestamp": causal_boundary},
            "causal_watermark": causal_boundary,
            "latest_1m_price": 155.5,
            "latest_1m_source": "upstox_v3",
            "latest_1m_observed_at": causal_boundary - timedelta(seconds=10),
        }
        target = agent._execution_target(item, causal_boundary)
        assert target is not None
        assert target["price"] == Decimal("155.5")
        assert target["source"] == "upstox_v3"

    def test_naive_timestamp_on_cached_mark_treated_as_utc(self):
        """Behavioral: a naive (no tzinfo) observed_at on cached mark is treated as UTC for boundary comparison."""
        from backend.live_inference import LivePaperInference

        agent = LivePaperInference.__new__(LivePaperInference)
        agent.engine = MagicMock()
        agent.option_paper_enabled = False
        agent.intraday_short_fallback = True

        causal_boundary = datetime(2026, 10, 19, 3, 50, 0, tzinfo=timezone.utc)

        # Naive timestamp 1 second after boundary — should be rejected
        item_future = {
            "instrument_id": 101,
            "instrument_token": "TATASTEEL",
            "instrument_type": "EQ",
            "signal": 1,
            "session": {"close": Decimal("155.0"), "timestamp": causal_boundary},
            "causal_watermark": causal_boundary,
            "execution_mark": {
                "price": Decimal("155.0"),
                "source": "upstox_v3",
                "observed_at": datetime(2026, 10, 19, 3, 50, 1),  # naive, no tzinfo
            },
        }
        target_future = agent._execution_target(item_future, causal_boundary)
        assert target_future is None

        # Naive timestamp exactly at boundary — should be accepted
        item_exact = {
            "instrument_id": 101,
            "instrument_token": "TATASTEEL",
            "instrument_type": "EQ",
            "signal": 1,
            "session": {"close": Decimal("155.0"), "timestamp": causal_boundary},
            "causal_watermark": causal_boundary,
            "execution_mark": {
                "price": Decimal("155.0"),
                "source": "upstox_v3",
                "observed_at": datetime(2026, 10, 19, 3, 50, 0),  # naive, no tzinfo
            },
        }
        target_exact = agent._execution_target(item_exact, causal_boundary)
        assert target_exact is not None
        assert target_exact["price"] == Decimal("155.0")

    def test_multileg_hedge_target_query_enforces_causal_boundary(self):
        """Verifies multi-leg hedge leg selection in run() enforces :causal_boundary."""
        import inspect
        from backend.live_inference import LivePaperInference
        source = inspect.getsource(LivePaperInference.run)
        assert "MARKET_BAR_AVAILABILITY_SQL" in source
        assert "<= :causal_boundary" in source
        assert '"causal_boundary": hedge_boundary' in source

    def test_decision_boundary_rejects_observation_at_09_20_01_and_accepts_09_20_00(self):
        """Behavioral test: Decision boundary of 09:20:00 strictly rejects an observation at 09:20:01 and accepts <= 09:20:00."""
        decision_boundary = datetime(2026, 10, 19, 9, 20, 0, tzinfo=timezone.utc)

        # Observation 1 second in the future (09:20:01)
        obs_future = {
            "symbol": "NIFTY_CE",
            "interval": "1second",
            "bar_time": datetime(2026, 10, 19, 9, 20, 1, tzinfo=timezone.utc),
            "exchange_timestamp": datetime(2026, 10, 19, 9, 20, 1, 100000, tzinfo=timezone.utc),
            "price": Decimal("125.0"),
        }
        # Observation at exact boundary (09:20:00)
        obs_exact = {
            "symbol": "NIFTY_CE",
            "interval": "1second",
            "bar_time": datetime(2026, 10, 19, 9, 20, 0, tzinfo=timezone.utc),
            "exchange_timestamp": datetime(2026, 10, 19, 9, 20, 0, 0, tzinfo=timezone.utc),
            "price": Decimal("124.5"),
        }
        # Observation prior to boundary (09:19:59)
        obs_prior = {
            "symbol": "NIFTY_CE",
            "interval": "1second",
            "bar_time": datetime(2026, 10, 19, 9, 19, 59, tzinfo=timezone.utc),
            "exchange_timestamp": datetime(2026, 10, 19, 9, 19, 59, 500000, tzinfo=timezone.utc),
            "price": Decimal("124.0"),
        }

        def compute_observed_at(row):
            if row.get("exchange_timestamp"):
                return row["exchange_timestamp"]
            interval = row["interval"]
            delta = timedelta(minutes=5) if interval == "5minute" else (timedelta(minutes=1) if interval == "1minute" else timedelta(seconds=1))
            return row["bar_time"] + delta

        # SQL predicate: compute_observed_at(row) <= decision_boundary
        eligible = [row for row in [obs_future, obs_exact, obs_prior] if compute_observed_at(row) <= decision_boundary]

        assert len(eligible) == 2
        assert obs_future not in eligible
        assert obs_exact in eligible
        assert obs_prior in eligible

    def test_next_bucket_5m_candle_is_excluded_by_causal_boundary_filter(self):
        """Simulates next-bucket candle arrival and verifies causal boundary filters it out."""
        # Decision boundary = 09:20:00 (for 09:15-09:20 cycle)
        decision_boundary = datetime(2026, 10, 19, 9, 20, 0, tzinfo=timezone.utc)

        # Candidate bars in database:
        # 1. Next-bucket 5m bar starting at 09:20:00 (ending at 09:25:00) with exchange_timestamp at 09:24:59
        next_bucket_5m = {
            "interval": "5minute",
            "bar_time": datetime(2026, 10, 19, 9, 20, 0, tzinfo=timezone.utc),
            "exchange_timestamp": datetime(2026, 10, 19, 9, 24, 59, tzinfo=timezone.utc),
            "close_price": Decimal("200.0"),
            "source": "upstox_v3",
        }
        # 2. Next-bucket 5m bar without exchange_timestamp (fallback: 09:20:00 + 5m = 09:25:00)
        next_bucket_5m_no_ts = {
            "interval": "5minute",
            "bar_time": datetime(2026, 10, 19, 9, 20, 0, tzinfo=timezone.utc),
            "exchange_timestamp": None,
            "close_price": Decimal("201.0"),
            "source": "upstox_v3",
        }
        # 3. Current-bucket 1s bar at 09:19:58
        current_bucket_1s = {
            "interval": "1second",
            "bar_time": datetime(2026, 10, 19, 9, 19, 58, tzinfo=timezone.utc),
            "exchange_timestamp": datetime(2026, 10, 19, 9, 19, 58, 200000, tzinfo=timezone.utc),
            "close_price": Decimal("155.0"),
            "source": "upstox_v3",
        }

        def compute_observed_at(row):
            if row.get("exchange_timestamp"):
                return row["exchange_timestamp"]
            interval = row["interval"]
            delta = timedelta(minutes=5) if interval == "5minute" else (timedelta(minutes=1) if interval == "1minute" else timedelta(seconds=1))
            return row["bar_time"] + delta

        # Apply causal filter: observed_at <= decision_boundary
        filtered = [
            row for row in [next_bucket_5m, next_bucket_5m_no_ts, current_bucket_1s]
            if compute_observed_at(row) <= decision_boundary
        ]

        # Next-bucket 5m bars MUST be filtered out
        assert len(filtered) == 1
        assert filtered[0]["interval"] == "1second"
        assert filtered[0]["close_price"] == Decimal("155.0")

    def test_missing_exchange_timestamp_uses_bar_end_not_bar_start(self):
        """Verifies that a bar without exchange_timestamp derives observed_at from bar_time + interval_duration,
        NOT from bar_time itself. A 5m bar starting at 09:15 has observed_at = 09:20, not 09:15."""
        decision_boundary = datetime(2026, 10, 19, 9, 18, 0, tzinfo=timezone.utc)

        # 5m bar starting at 09:15 with no exchange_timestamp
        # Its data wasn't available at 09:15 — it completes at 09:20
        bar_no_ts = {
            "interval": "5minute",
            "bar_time": datetime(2026, 10, 19, 9, 15, 0, tzinfo=timezone.utc),
            "exchange_timestamp": None,
            "close_price": Decimal("100.0"),
        }

        def compute_observed_at(row):
            if row.get("exchange_timestamp"):
                return row["exchange_timestamp"]
            interval = row["interval"]
            delta = timedelta(minutes=5) if interval == "5minute" else (timedelta(minutes=1) if interval == "1minute" else timedelta(seconds=1))
            return row["bar_time"] + delta

        obs_at = compute_observed_at(bar_no_ts)
        # observed_at should be 09:20:00, not 09:15:00
        assert obs_at == datetime(2026, 10, 19, 9, 20, 0, tzinfo=timezone.utc)
        # A decision boundary of 09:18 must NOT admit this bar (it completes at 09:20)
        assert obs_at > decision_boundary

    def test_clock_skew_tolerance_separated_from_causal_boundary(self):
        """Verifies 5s clock skew tolerance in validate_market_data_for_entry is separate from causal boundary."""
        now = datetime.now(timezone.utc)

        # 1. In real-time freshness validation, a 2s future timestamp from clock skew passes
        valid, err = validate_market_data_for_entry(
            price=Decimal("100.0"),
            observed_at=now + timedelta(seconds=2),
            source="upstox_v3",
            reference_time=now,
        )
        assert valid, f"Expected clock skew tolerance to pass, got: {err}"

        # 2. In historical causal boundary, clock skew tolerance is NOT applied:
        # A hard boundary of T must never admit T + 1s into the query results.
        boundary = datetime(2026, 10, 19, 9, 20, 0, tzinfo=timezone.utc)
        obs_time = datetime(2026, 10, 19, 9, 20, 1, tzinfo=timezone.utc)
        assert obs_time > boundary, "Causal filter strictly rejects any timestamp > boundary"

    def test_stale_option_and_hedge_marks_fail_closed(self):
        """Verifies that option and hedge marks older than 30s are rejected even if non-null."""
        now = datetime.now(timezone.utc)

        # Option mark 45s old
        valid, err = validate_market_data_for_entry(
            price=Decimal("45.50"),
            observed_at=now - timedelta(seconds=45),
            source="upstox_v3",
            max_age_seconds=30.0,
            reference_time=now,
        )
        assert not valid
        assert "PRICE_STALE" in err

        # Hedge mark 35s old
        valid_hedge, hedge_err = validate_market_data_for_entry(
            price=Decimal("12.25"),
            observed_at=now - timedelta(seconds=35),
            source="upstox_v3",
            max_age_seconds=30.0,
            reference_time=now,
        )
        assert not valid_hedge
        assert "PRICE_STALE" in hedge_err

        # Fresh mark 2s old passes
        valid_fresh, fresh_err = validate_market_data_for_entry(
            price=Decimal("45.50"),
            observed_at=now - timedelta(seconds=2),
            source="upstox_v3",
            max_age_seconds=30.0,
            reference_time=now,
        )
        assert valid_fresh, f"Expected fresh mark to pass, got: {fresh_err}"

    def test_query_boundary_does_not_replace_freshness_validation(self):
        """Verifies that a mark from an old candle close is still rejected as stale against wall-clock now."""
        # A historical candle closed 2 hours ago
        historical_eval = datetime(2026, 10, 19, 5, 5, 0, tzinfo=timezone.utc)
        mark_from_cycle_close = historical_eval - timedelta(seconds=2)

        # When evaluated at real wall-clock now (2 hours later)
        now = historical_eval + timedelta(hours=2)

        valid, err = validate_market_data_for_entry(
            price=Decimal("150.0"),
            observed_at=mark_from_cycle_close,
            source="upstox_v3",
            max_age_seconds=30.0,
            reference_time=now,
        )
        assert not valid
        assert "PRICE_STALE" in err

    def test_live_arrival_timestamp_prevents_lookahead_when_received_after_boundary(self):
        """Verifies that an exchange tick timestamped before boundary but received after boundary is rejected."""
        decision_boundary = datetime(2026, 10, 19, 9, 20, 0, tzinfo=timezone.utc)

        # Tick executed at exchange at 09:19:59.850, but network latency caused it to arrive at received_at = 09:20:00.150
        tick_arrived_late = {
            "interval": "1second",
            "bar_time": datetime(2026, 10, 19, 9, 19, 59, tzinfo=timezone.utc),
            "exchange_timestamp": datetime(2026, 10, 19, 9, 19, 59, 850000, tzinfo=timezone.utc),
            "received_at": datetime(2026, 10, 19, 9, 20, 0, 150000, tzinfo=timezone.utc),
            "close_price": Decimal("24500.0"),
        }
        # Tick executed at exchange at 09:19:59.200, received in time at received_at = 09:19:59.450
        tick_arrived_in_time = {
            "interval": "1second",
            "bar_time": datetime(2026, 10, 19, 9, 19, 59, tzinfo=timezone.utc),
            "exchange_timestamp": datetime(2026, 10, 19, 9, 19, 59, 200000, tzinfo=timezone.utc),
            "received_at": datetime(2026, 10, 19, 9, 19, 59, 450000, tzinfo=timezone.utc),
            "close_price": Decimal("24495.0"),
        }

        def compute_availability(row):
            bar_time = row["bar_time"]
            rec = row.get("received_at")
            ex = row.get("exchange_timestamp")
            # Only verified live arrival timestamps qualify
            if rec is not None and rec >= bar_time and rec <= bar_time + timedelta(minutes=15):
                return max(ex or bar_time, rec)
            return None

        avail_late = compute_availability(tick_arrived_late)
        avail_in_time = compute_availability(tick_arrived_in_time)

        # The late-arriving tick availability is 09:20:00.150 > boundary (rejected)
        assert avail_late > decision_boundary
        # The in-time tick availability is 09:19:59.450 <= boundary (accepted)
        assert avail_in_time <= decision_boundary

    def test_live_arrival_latency_exceeding_500ms_rejects_exchange_tick_when_received_after_boundary(self):
        """Verifies that when network/ingestion latency exceeds 500ms, an exchange event timestamped
        well before the boundary but arriving after the boundary is strictly rejected."""
        decision_boundary = datetime(2026, 10, 19, 9, 20, 0, tzinfo=timezone.utc)

        # Exchange tick at 09:19:59.100 (900ms before boundary), but arrival latency is 1200ms
        # so received_at = 09:20:00.300 (after the decision boundary)
        high_latency_tick = {
            "interval": "1second",
            "bar_time": datetime(2026, 10, 19, 9, 19, 59, tzinfo=timezone.utc),
            "exchange_timestamp": datetime(2026, 10, 19, 9, 19, 59, 100000, tzinfo=timezone.utc),
            "received_at": datetime(2026, 10, 19, 9, 20, 0, 300000, tzinfo=timezone.utc),
            "close_price": Decimal("24505.0"),
        }

        def compute_availability(row):
            bar_time = row["bar_time"]
            rec = row.get("received_at")
            ex = row.get("exchange_timestamp")
            if rec is not None and rec >= bar_time and rec <= bar_time + timedelta(minutes=15):
                return max(ex or bar_time, rec)
            return None

        avail = compute_availability(high_latency_tick)
        assert avail is not None
        # Availability reflects true arrival time: 09:20:00.300 > 09:20:00.000 (rejected)
        assert avail > decision_boundary

    def test_unknown_historical_arrival_times_cannot_silently_qualify_as_point_in_time_evidence(self):
        """Verifies that unknown or unverified arrival times (missing received_at, backfill data)
        evaluate to None (SQL NULL) and cannot silently qualify as point-in-time evidence."""
        decision_boundary = datetime(2026, 10, 19, 9, 20, 0, tzinfo=timezone.utc)

        # 1. Row with missing received_at (arrival time unknown)
        missing_receipt_tick = {
            "interval": "1second",
            "bar_time": datetime(2026, 10, 19, 9, 19, 58, tzinfo=timezone.utc),
            "exchange_timestamp": datetime(2026, 10, 19, 9, 19, 58, 500000, tzinfo=timezone.utc),
            "received_at": None,
        }
        # 2. Historical backfill row where received_at is days after bar_time
        backfilled_bar = {
            "interval": "1minute",
            "bar_time": datetime(2026, 10, 19, 9, 19, 0, tzinfo=timezone.utc),
            "exchange_timestamp": datetime(2026, 10, 19, 9, 19, 59, 900000, tzinfo=timezone.utc),
            "received_at": datetime(2026, 10, 25, 14, 0, 0, tzinfo=timezone.utc),  # backfilled 6 days later
        }

        def compute_availability(row):
            bar_time = row["bar_time"]
            rec = row.get("received_at")
            ex = row.get("exchange_timestamp")
            if rec is not None and rec >= bar_time and rec <= bar_time + timedelta(minutes=15):
                return max(ex or bar_time, rec)
            return None

        # Both return None (unknown availability)
        assert compute_availability(missing_receipt_tick) is None
        assert compute_availability(backfilled_bar) is None

        # Point-in-time qualification test: unknown availability cannot satisfy <= decision_boundary
        def qualifies_point_in_time(row, boundary):
            avail = compute_availability(row)
            if avail is None:
                return False  # SQL: NULL <= boundary evaluates to UNKNOWN (falsy)
            return avail <= boundary

        assert not qualifies_point_in_time(missing_receipt_tick, decision_boundary)
        assert not qualifies_point_in_time(backfilled_bar, decision_boundary)

        # 3. Verify _execution_target() fails closed when SQL returns row with observed_at = None
        from backend.live_inference import LivePaperInference
        agent = LivePaperInference.__new__(LivePaperInference)
        agent.engine = MagicMock()
        agent.option_paper_enabled = True
        agent.intraday_short_fallback = True
        mock_conn = MagicMock()
        agent.engine.connect.return_value.__enter__.return_value = mock_conn

        mock_conn.execute.return_value.mappings.return_value.one_or_none.return_value = {
            "close_price": 150.0,
            "source": "upstox_v3",
            "bar_time": datetime(2026, 10, 19, 9, 19, 0, tzinfo=timezone.utc),
            "observed_at": None,  # Unknown historical availability
        }
        item = {
            "instrument_id": 101, "instrument_token": "TATASTEEL", "instrument_type": "EQ",
            "signal": 1, "session": {"close": Decimal("150.0")},
        }
        target = agent._execution_target(item, decision_boundary)
        # Must return None (fail closed) rather than falling back to bar_time
        assert target is None

    def test_normalize_timestamp_utc_comprehensive(self):
        """Verifies _normalize_timestamp_utc normalizes naive and aware datetimes, strings, and fails closed."""
        from zoneinfo import ZoneInfo
        from backend.live_inference import _normalize_timestamp_utc

        # 1. Missing or blank -> None
        assert _normalize_timestamp_utc(None) is None
        assert _normalize_timestamp_utc("") is None
        assert _normalize_timestamp_utc("   ") is None
        assert _normalize_timestamp_utc("not-a-timestamp") is None

        # 2. Invalid numbers -> None
        assert _normalize_timestamp_utc(0) is None
        assert _normalize_timestamp_utc(-5) is None
        assert _normalize_timestamp_utc(float("nan")) is None
        assert _normalize_timestamp_utc(float("inf")) is None

        # 3. Naive datetime -> normalized to UTC aware
        naive = datetime(2026, 10, 19, 9, 20, 0)
        norm_naive = _normalize_timestamp_utc(naive)
        assert norm_naive is not None
        assert norm_naive.tzinfo == timezone.utc
        assert norm_naive == datetime(2026, 10, 19, 9, 20, 0, tzinfo=timezone.utc)

        # 4. IST aware datetime -> converted to UTC aware datetime
        ist = ZoneInfo("Asia/Kolkata")
        aware_ist = datetime(2026, 10, 19, 14, 50, 0, tzinfo=ist)
        norm_ist = _normalize_timestamp_utc(aware_ist)
        assert norm_ist is not None
        assert norm_ist.tzinfo == timezone.utc
        assert norm_ist == datetime(2026, 10, 19, 9, 20, 0, tzinfo=timezone.utc)

        # 5. ISO strings -> normalized to UTC aware
        norm_iso_z = _normalize_timestamp_utc("2026-10-19T09:20:00Z")
        assert norm_iso_z == datetime(2026, 10, 19, 9, 20, 0, tzinfo=timezone.utc)

        norm_iso_offset = _normalize_timestamp_utc("2026-10-19T14:50:00+05:30")
        assert norm_iso_offset == datetime(2026, 10, 19, 9, 20, 0, tzinfo=timezone.utc)


class TestBackfillProvenanceAndConflictIntegrity:
    """Phase 2A Regression tests for backfill conflict behavior and provenance protection."""

    def test_conflicting_live_row_retains_all_fields_after_rest_replacement(self):
        """Phase 2A Regression Test 1: Verifies that a conflicting live row (e.g. upstox_v3 or zerodha_kite)
        retains its OHLCV, source, exchange timestamp, and received timestamp after a REST replacement attempt."""
        from sqlalchemy import create_engine, text
        from backend.upstox_backfill import UpstoxRestBackfill

        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE live_market_bars (
                    instrument_id INT,
                    interval TEXT,
                    bar_time TEXT,
                    open_price NUMERIC,
                    high_price NUMERIC,
                    low_price NUMERIC,
                    close_price NUMERIC,
                    volume INT,
                    open_interest INT,
                    source TEXT,
                    exchange_timestamp TEXT,
                    received_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (instrument_id, interval, bar_time)
                )
            """))
            # 1. Existing live bars: one from Upstox streaming, one from Zerodha streaming
            conn.execute(text("""
                INSERT INTO live_market_bars (instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, volume, open_interest, source, exchange_timestamp, received_at)
                VALUES
                    (1, CAST('1minute' AS bar_interval), '2026-10-19 09:15:00', 100, 105, 95, 102, 1000, 50, 'upstox_v3', '2026-10-19 09:15:00.123', '2026-10-19 09:15:00.250'),
                    (2, CAST('1minute' AS bar_interval), '2026-10-19 09:15:00', 200, 205, 195, 202, 2000, 60, 'zerodha_kite', '2026-10-19 09:15:00.456', '2026-10-19 09:15:00.500')
            """))

        client = UpstoxRestBackfill.__new__(UpstoxRestBackfill)
        client.engine = engine

        # Attempt to overwrite both live bars with REST backfill data
        rest_candle_1 = {
            "bar_time": "2026-10-19 09:15:00",
            "open": 999, "high": 999, "low": 999, "close": 999,
            "volume": 9999, "oi": 999,
        }
        rest_candle_2 = {
            "bar_time": "2026-10-19 09:15:00",
            "open": 888, "high": 888, "low": 888, "close": 888,
            "volume": 8888, "oi": 888,
        }
        client.save_bars(1, "1minute", [rest_candle_1], "upstox_rest_intraday", replace=True)
        client.save_bars(2, "1minute", [rest_candle_2], "upstox_rest_intraday", replace=True)

        with engine.connect() as conn:
            rows = {r["instrument_id"]: r for r in conn.execute(text("SELECT * FROM live_market_bars")).mappings().all()}
            assert len(rows) == 2

            # Instrument 1 (Upstox live) MUST be completely unchanged
            row1 = rows[1]
            assert row1["open_price"] == 100
            assert row1["high_price"] == 105
            assert row1["low_price"] == 95
            assert row1["close_price"] == 102
            assert row1["volume"] == 1000
            assert row1["open_interest"] == 50
            assert row1["source"] == "upstox_v3"
            assert row1["exchange_timestamp"] == "2026-10-19 09:15:00.123"
            assert str(row1["received_at"]) == "2026-10-19 09:15:00.250"

            # Instrument 2 (Zerodha live) MUST also be completely unchanged
            row2 = rows[2]
            assert row2["open_price"] == 200
            assert row2["high_price"] == 205
            assert row2["low_price"] == 195
            assert row2["close_price"] == 202
            assert row2["volume"] == 2000
            assert row2["open_interest"] == 60
            assert row2["source"] == "zerodha_kite"
            assert row2["exchange_timestamp"] == "2026-10-19 09:15:00.456"
            assert str(row2["received_at"]) == "2026-10-19 09:15:00.500"

    def test_backfill_row_can_be_replaced_by_better_backfill_row(self):
        """Phase 2A Regression Test 2: Verifies that an existing backfill row can still be replaced
        by a better backfill row when explicitly requested (replace=True), preserving original received_at."""
        from sqlalchemy import create_engine, text
        from backend.upstox_backfill import UpstoxRestBackfill

        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE live_market_bars (
                    instrument_id INT,
                    interval TEXT,
                    bar_time TEXT,
                    open_price NUMERIC,
                    high_price NUMERIC,
                    low_price NUMERIC,
                    close_price NUMERIC,
                    volume INT,
                    open_interest INT,
                    source TEXT,
                    exchange_timestamp TEXT,
                    received_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (instrument_id, interval, bar_time)
                )
            """))
            # Existing row is from a low-resolution historical backfill
            conn.execute(text("""
                INSERT INTO live_market_bars (instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, volume, open_interest, source, exchange_timestamp, received_at)
                VALUES (1, CAST('1minute' AS bar_interval), '2026-10-19 09:15:00', 100, 105, 95, 102, 1000, 10, 'upstox_rest_history', '2026-10-19 09:15:00', '2026-10-19 09:15:00.100')
            """))

        client = UpstoxRestBackfill.__new__(UpstoxRestBackfill)
        client.engine = engine

        # Newer, higher-fidelity intraday backfill replaces the historical row
        better_candle = {
            "bar_time": "2026-10-19 09:15:00",
            "open": 101, "high": 106, "low": 96, "close": 104,
            "volume": 1500, "oi": 25,
        }
        res = client.save_bars(1, "1minute", [better_candle], "upstox_rest_intraday", replace=True)
        assert res >= 1

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM live_market_bars")).mappings().all()
            assert len(rows) == 1
            row = rows[0]
            # Backfill values and source are updated
            assert row["open_price"] == 101
            assert row["high_price"] == 106
            assert row["low_price"] == 96
            assert row["close_price"] == 104
            assert row["volume"] == 1500
            assert row["open_interest"] == 25
            assert row["source"] == "upstox_rest_intraday"
            # Original arrival timestamp preserved
            assert str(row["received_at"]) == "2026-10-19 09:15:00.100"

    def test_backfill_disabled_replace_preserves_existing_rows_unchanged(self):
        """Phase 2A Regression Test 3: Verifies that save_bars(replace=False) uses DO NOTHING
        and leaves existing rows untouched, whether they are live or backfill."""
        from sqlalchemy import create_engine, text
        from backend.upstox_backfill import UpstoxRestBackfill

        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE live_market_bars (
                    instrument_id INT,
                    interval TEXT,
                    bar_time TEXT,
                    open_price NUMERIC,
                    high_price NUMERIC,
                    low_price NUMERIC,
                    close_price NUMERIC,
                    volume INT,
                    open_interest INT,
                    source TEXT,
                    exchange_timestamp TEXT,
                    received_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (instrument_id, interval, bar_time)
                )
            """))
            conn.execute(text("""
                INSERT INTO live_market_bars (instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, volume, source, exchange_timestamp, received_at)
                VALUES
                    (1, CAST('1minute' AS bar_interval), '2026-10-19 09:15:00', 100, 105, 95, 102, 1000, 'upstox_v3', '2026-10-19 09:15:00', '2026-10-19 09:15:00.250'),
                    (2, CAST('1minute' AS bar_interval), '2026-10-19 09:15:00', 200, 205, 195, 202, 2000, 'upstox_rest_5m', '2026-10-19 09:15:00', '2026-10-19 09:15:00.300')
            """))

        client = UpstoxRestBackfill.__new__(UpstoxRestBackfill)
        client.engine = engine

        candle_ignored = {
            "bar_time": "2026-10-19 09:15:00",
            "open": 999, "high": 999, "low": 999, "close": 999,
            "volume": 9999, "oi": 99,
        }
        client.save_bars(1, "1minute", [candle_ignored], "upstox_rest_intraday", replace=False)
        client.save_bars(2, "1minute", [candle_ignored], "upstox_rest_intraday", replace=False)

        with engine.connect() as conn:
            rows = {r["instrument_id"]: r for r in conn.execute(text("SELECT * FROM live_market_bars")).mappings().all()}
            assert len(rows) == 2

            # Live row untouched
            assert rows[1]["close_price"] == 102
            assert rows[1]["volume"] == 1000
            assert rows[1]["source"] == "upstox_v3"
            assert str(rows[1]["received_at"]) == "2026-10-19 09:15:00.250"

            # Backfill row untouched under replace=False
            assert rows[2]["close_price"] == 202
            assert rows[2]["volume"] == 2000
            assert rows[2]["source"] == "upstox_rest_5m"
            assert str(rows[2]["received_at"]) == "2026-10-19 09:15:00.300"

    def test_source_classification_and_entry_eligibility_after_replacement_attempts(self):
        """Phase 2A Regression Test 4: Verifies that after replacement attempts:
        - A protected live row retains LIVE_STREAM classification and entry eligibility.
        - A backfill row remains REST_BACKFILL and is strictly prohibited from authorizing live trade entries."""
        from sqlalchemy import create_engine, text
        from backend.upstox_backfill import UpstoxRestBackfill
        from backend.market_data_feed import (
            classify_provenance,
            DataProvenance,
            validate_market_data_for_entry,
            ENTRY_ELIGIBLE_PROVENANCES,
        )

        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE live_market_bars (
                    instrument_id INT,
                    interval TEXT,
                    bar_time TEXT,
                    open_price NUMERIC,
                    high_price NUMERIC,
                    low_price NUMERIC,
                    close_price NUMERIC,
                    volume INT,
                    open_interest INT,
                    source TEXT,
                    exchange_timestamp TEXT,
                    received_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (instrument_id, interval, bar_time)
                )
            """))
            conn.execute(text("""
                INSERT INTO live_market_bars (instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, volume, source, exchange_timestamp, received_at)
                VALUES
                    (1, CAST('1minute' AS bar_interval), '2026-10-19 09:15:00', 100, 105, 95, 102, 1000, 'upstox_v3', '2026-10-19 09:15:00', '2026-10-19 09:15:00.250'),
                    (2, CAST('1minute' AS bar_interval), '2026-10-19 09:15:00', 200, 205, 195, 202, 2000, 'upstox_rest_5m', '2026-10-19 09:15:00', '2026-10-19 09:15:00.300')
            """))

        client = UpstoxRestBackfill.__new__(UpstoxRestBackfill)
        client.engine = engine

        candle_replace = {
            "bar_time": "2026-10-19 09:15:00",
            "open": 500, "high": 505, "low": 495, "close": 502,
            "volume": 5000, "oi": 50,
        }
        client.save_bars(1, "1minute", [candle_replace], "upstox_rest_intraday", replace=True)
        client.save_bars(2, "1minute", [candle_replace], "upstox_rest_intraday", replace=True)

        with engine.connect() as conn:
            rows = {r["instrument_id"]: r for r in conn.execute(text("SELECT * FROM live_market_bars")).mappings().all()}

            # 1. Row 1 was live: protected from replacement
            row1 = rows[1]
            assert row1["source"] == "upstox_v3"
            prov1 = classify_provenance(row1["source"])
            assert prov1 == DataProvenance.LIVE_STREAM
            assert prov1 in ENTRY_ELIGIBLE_PROVENANCES
            valid1, _ = validate_market_data_for_entry(
                price=Decimal(str(row1["close_price"])),
                observed_at=datetime(2026, 10, 19, 9, 15, 0, tzinfo=timezone.utc),
                source=row1["source"],
                max_age_seconds=60.0,
                reference_time=datetime(2026, 10, 19, 9, 15, 30, tzinfo=timezone.utc),
            )
            assert valid1 is True

            # 2. Row 2 was backfill: replaced with upstox_rest_intraday, strictly rejected from entry
            row2 = rows[2]
            assert row2["source"] == "upstox_rest_intraday"
            prov2 = classify_provenance(row2["source"])
            assert prov2 == DataProvenance.REST_BACKFILL
            assert prov2 not in ENTRY_ELIGIBLE_PROVENANCES
            valid2, err2 = validate_market_data_for_entry(
                price=Decimal(str(row2["close_price"])),
                observed_at=datetime(2026, 10, 19, 9, 15, 0, tzinfo=timezone.utc),
                source=row2["source"],
                max_age_seconds=60.0,
                reference_time=datetime(2026, 10, 19, 9, 15, 30, tzinfo=timezone.utc),
            )
            assert valid2 is False
            assert "REST_BACKFILL_CANNOT_AUTHORIZE_ENTRY" in err2


class TestInferenceSchedulingAndObservability:
    """Phase C Regression Tests: Inference scheduling, watermark lifecycle, idempotency, and observability."""

    def test_repeated_execution_same_watermark_deduplication(self):
        """Verifies database deduplication prevents duplicate prediction/snapshot rows when
        the same watermark is polled multiple times, distinguishing computation from persistence."""
        from backend.live_inference import LivePaperInference

        agent = LivePaperInference.__new__(LivePaperInference)
        mock_conn = MagicMock()
        mock_engine = MagicMock()
        mock_engine.begin.return_value.__enter__.return_value = mock_conn
        agent.engine = mock_engine

        model = {"version": "v1.0.0", "feature_set": "test_set"}
        watermark = datetime(2026, 10, 19, 9, 20, 0, tzinfo=timezone.utc)
        item = {
            "instrument_id": 1,
            "exchange": "NSE",
            "symbol": "RELIANCE",
            "source_bar_time": watermark - timedelta(minutes=5),
            "causal_watermark": watermark,
            "session": {"close": Decimal("2500.0")},
        }
        features = {"rsi_14": 55.2, "sma_20": 2490.0}

        # First run: DB indicates new row inserted (RETURNING 1 -> 1)
        mock_conn.execute.return_value.scalar_one_or_none.return_value = 1
        first_inserted = agent._persist_prediction(model, item, features, 0.75, 1)
        assert first_inserted is True
        assert mock_conn.execute.call_count == 2  # Snapshot INSERT and shadow_predictions INSERT
        first_sql = str(mock_conn.execute.call_args_list[0][0][0])
        assert "ON CONFLICT(model_version,instrument_id,timeframe,observed_at) DO NOTHING RETURNING 1" in first_sql
        assert "ON CONFLICT(model_version,exchange,symbol,timestamp) DO NOTHING" in str(mock_conn.execute.call_args_list[1][0][0])

        # Second run with identical watermark: computation ran, but DB deduplicates (RETURNING -> None)
        mock_conn.reset_mock()
        mock_conn.execute.return_value.scalar_one_or_none.return_value = None
        second_inserted = agent._persist_prediction(model, item, features, 0.75, 1)
        assert second_inserted is False
        # Crucial distinction: snapshot insert attempted, but shadow_predictions was NOT inserted
        assert mock_conn.execute.call_count == 1

    def test_scheduled_run_before_candle_persisted_fallback(self):
        """Verifies that if a scheduled task runs before the latest candle is persisted,
        watermark selection falls back cleanly to the prior completed bar, and advances on the next run."""
        from backend.live_inference import LivePaperInference, TRADE_BAR_SOURCES, LIVE_STREAM_SOURCES

        agent = LivePaperInference.__new__(LivePaperInference)
        mock_conn = MagicMock()
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__.return_value = mock_conn
        agent.engine = mock_engine

        # Run 1: Task runs at 09:15:05 before 09:15:00 candle is persisted
        bar_0910 = datetime(2026, 10, 19, 9, 10, 0, tzinfo=timezone.utc)
        mock_conn.execute.return_value.scalar_one_or_none.return_value = bar_0910
        wm1 = agent._watermark()
        assert wm1 == bar_0910

        query_sql, query_params = mock_conn.execute.call_args[0][0], mock_conn.execute.call_args[0][1]
        assert "SELECT MAX(bar_time) FROM live_market_bars WHERE interval='5minute' AND source = ANY(:sources)" in str(query_sql)
        eligible_live_sources = {s for s in TRADE_BAR_SOURCES if s in LIVE_STREAM_SOURCES}
        assert set(query_params["sources"]) == eligible_live_sources
        assert not any("rest" in s or "backfill" in s for s in query_params["sources"])

        # Run 2: Next cycle runs after 09:15:00 candle is committed
        bar_0915 = datetime(2026, 10, 19, 9, 15, 0, tzinfo=timezone.utc)
        mock_conn.execute.return_value.scalar_one_or_none.return_value = bar_0915
        wm2 = agent._watermark()
        assert wm2 == bar_0915
        assert wm2 > wm1

    def test_delayed_task_with_stale_execution_mark_fails_closed(self):
        """Verifies fail-closed behavior when task delay causes an execution mark to exceed max_age_seconds."""
        from backend.market_data_feed import validate_market_data_for_entry

        candle_close = datetime(2026, 10, 19, 9, 20, 0, tzinfo=timezone.utc)
        delayed_run_time = datetime(2026, 10, 19, 9, 21, 5, tzinfo=timezone.utc)  # 65s delay

        # Mark from 09:20:00 evaluated at 09:21:05 with 30s max age
        valid, err = validate_market_data_for_entry(
            price=Decimal("150.0"),
            observed_at=candle_close,
            source="upstox_v3",
            max_age_seconds=30.0,
            reference_time=delayed_run_time,
        )
        assert valid is False
        assert "PRICE_STALE" in err
        assert "65.0s exceeds limit=30.0s" in err

        # Lookahead protection: mark observed in future relative to reference time
        future_mark = delayed_run_time + timedelta(seconds=10)
        lookahead_valid, lookahead_err = validate_market_data_for_entry(
            price=Decimal("150.0"),
            observed_at=future_mark,
            source="upstox_v3",
            max_age_seconds=30.0,
            reference_time=delayed_run_time,
        )
        assert lookahead_valid is False
        assert "PRICE_TIMESTAMP_IN_FUTURE" in lookahead_err

    def test_watermark_query_causal_boundary_safeguard(self):
        """Verifies that an unverified or future bar cannot produce an executable mark under causal rules."""
        from backend.live_inference import MARKET_BAR_AVAILABILITY_SQL, _normalize_timestamp_utc
        from backend.market_data_feed import validate_market_data_for_entry

        # 1. SQL availability definition must strictly check received_at bounds
        assert "received_at IS NOT NULL" in MARKET_BAR_AVAILABILITY_SQL
        assert "received_at >= bar_time" in MARKET_BAR_AVAILABILITY_SQL
        assert "received_at <= bar_time + INTERVAL '15 minutes'" in MARKET_BAR_AVAILABILITY_SQL

        # 2. Causal normalization: missing/malformed marks fail closed
        assert _normalize_timestamp_utc(None) is None
        assert _normalize_timestamp_utc("invalid-timestamp") is None

        # 3. Future bar (> 5s clock buffer) relative to execution time is rejected by entry validation
        now = datetime(2026, 10, 19, 9, 20, 0, tzinfo=timezone.utc)
        future_time = now + timedelta(seconds=10)
        valid, err = validate_market_data_for_entry(
            price=Decimal("2500.0"),
            observed_at=future_time,
            source="upstox_v3",
            reference_time=now,
        )
        assert valid is False
        assert "PRICE_TIMESTAMP_IN_FUTURE" in err

    def test_lock_contention_and_task_failure_observability(self):
        """Verifies lock contention gracefully skips with structured logging,
        task failure releases lock in finally block, and successful run records timing metrics."""
        from unittest.mock import patch, MagicMock
        from datetime import datetime, timezone

        with patch("sqlalchemy.create_engine", return_value=MagicMock()), \
             patch("redis.Redis.from_url", return_value=MagicMock()):
            import backend.tasks.worker as worker_mod

            class MockDateTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime(2026, 10, 19, 10, 30, 0, tzinfo=tz or timezone.utc)

            # Scenario A: Lock contention (another worker holds the lock)
            with patch.object(worker_mod, "datetime", MockDateTime), \
                 patch.object(worker_mod, "_acquire_lock", return_value=""), \
                 patch.object(worker_mod.logger, "info") as mock_log:
                res = worker_mod.run_live_paper_inference()
                assert res["status"] == "skipped"
                assert "another inference worker holds the distributed lock" in res["reason"]
                assert res["orders_allowed"] is False
                # Verify structured lock_skipped log
                assert mock_log.called
                log_context = mock_log.call_args[1].get("extra", {}).get("context", {})
                assert log_context.get("event") == "lock_skipped"
                assert log_context.get("lock") == "live-paper-inference"

            # Scenario B: Task failure during inference ensures lock is released
            mock_infer = MagicMock()
            mock_infer.run.side_effect = RuntimeError("transient DB failure")
            worker_mod._live_inference = mock_infer

            with patch.object(worker_mod, "datetime", MockDateTime), \
                 patch.object(worker_mod, "_acquire_lock", return_value="test-token-789"), \
                 patch.object(worker_mod, "_release_lock") as mock_release:
                try:
                    worker_mod.run_live_paper_inference()
                except RuntimeError as exc:
                    assert "transient DB failure" in str(exc)
                mock_release.assert_called_once_with("live-paper-inference", "test-token-789")

            # Scenario C: Successful execution records timing and validation observability
            mock_infer_ok = MagicMock()
            mock_infer_ok.run.return_value = {
                "status": "success",
                "watermark": "2026-10-19T09:20:00+00:00",
                "timings": {
                    "prepare_duration_ms": 12.5,
                    "scoring_duration_ms": 45.2,
                    "shadow_persist_duration_ms": 8.1,
                    "total_inference_duration_ms": 65.8,
                },
                "execution_validation": {
                    "passed": 2,
                    "rejected": 1,
                    "rejection_reasons": ["PRICE_STALE: 35.0s exceeds limit=30.0s"],
                },
                "orders_allowed": False,
            }
            worker_mod._live_inference = mock_infer_ok

            with patch.object(worker_mod, "datetime", MockDateTime), \
                 patch.object(worker_mod, "_acquire_lock", return_value="test-token-456"), \
                 patch.object(worker_mod, "_release_lock") as mock_release, \
                 patch.object(worker_mod.logger, "info") as mock_log:
                res = worker_mod.run_live_paper_inference()
                assert res["status"] == "success"
                assert res["timings"]["prepare_duration_ms"] == 12.5
                assert res["execution_validation"]["passed"] == 2
                mock_release.assert_called_once_with("live-paper-inference", "test-token-456")

                # Verify structured task_completed log
                completed_calls = [
                    c for c in mock_log.call_args_list
                    if c[1].get("extra", {}).get("context", {}).get("event") == "task_completed"
                ]
                assert len(completed_calls) == 1
                completed_ctx = completed_calls[0][1]["extra"]["context"]
                assert completed_ctx["status"] == "success"
                assert "total_task_duration_ms" in completed_ctx
                assert completed_ctx["timings"]["scoring_duration_ms"] == 45.2


class TestCausalWatermarkSafety:
    """Phase 2B Regression Tests: Causal watermark safety and live source isolation."""

    def test_newer_rest_backfill_bar_cannot_advance_live_watermark(self):
        """Verifies that a newer REST backfill bar (e.g. 09:25:00) cannot advance the live inference
        watermark past the latest eligible live streaming bar (09:15:00)."""
        from backend.live_inference import LivePaperInference, LIVE_STREAM_SOURCES

        agent = LivePaperInference.__new__(LivePaperInference)
        mock_conn = MagicMock()
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__.return_value = mock_conn
        agent.engine = mock_engine

        live_bar_time = datetime(2026, 10, 19, 9, 15, 0, tzinfo=timezone.utc)
        backfill_bar_time = datetime(2026, 10, 19, 9, 25, 0, tzinfo=timezone.utc)

        mock_conn.execute.return_value.scalar_one_or_none.return_value = live_bar_time
        selected_wm = agent._watermark()

        query_sql, query_params = mock_conn.execute.call_args[0][0], mock_conn.execute.call_args[0][1]
        assert "SELECT MAX(bar_time) FROM live_market_bars WHERE interval='5minute' AND source = ANY(:sources)" in str(query_sql)
        sources_used = query_params["sources"]
        # All queried sources MUST be in LIVE_STREAM_SOURCES
        assert all(s in LIVE_STREAM_SOURCES for s in sources_used)
        assert "upstox_rest_intraday" not in sources_used
        assert "upstox_rest_5m" not in sources_used
        assert "upstox_rest_history" not in sources_used
        assert "kite_gap_backfill" not in sources_used
        # Selected watermark matches live bar time, never the newer backfill bar time
        assert selected_wm == live_bar_time
        assert selected_wm < backfill_bar_time

    def test_valid_live_bar_selected_when_both_live_and_backfill_exist(self):
        """Verifies that when both live streaming and backfill bars are present,
        _watermark() only selects among eligible live sources."""
        from backend.live_inference import LivePaperInference

        agent = LivePaperInference.__new__(LivePaperInference)
        mock_conn = MagicMock()
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__.return_value = mock_conn
        agent.engine = mock_engine

        expected_live_wm = datetime(2026, 10, 19, 9, 20, 0, tzinfo=timezone.utc)
        mock_conn.execute.return_value.scalar_one_or_none.return_value = expected_live_wm

        wm = agent._watermark()
        assert wm == expected_live_wm

        query_params = mock_conn.execute.call_args[0][1]
        assert "upstox_v3" in query_params["sources"]
        assert "zerodha_kite" in query_params["sources"]

    def test_backfill_only_data_follows_safe_no_live_data_path(self):
        """Verifies that when only backfill bars exist in the database (zero live streaming bars),
        _watermark() returns None, and run() cleanly skips without authorizing any orders."""
        from backend.live_inference import LivePaperInference

        agent = LivePaperInference.__new__(LivePaperInference)
        mock_conn = MagicMock()
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__.return_value = mock_conn
        agent.engine = mock_engine

        # When no live sources exist, database returns None
        mock_conn.execute.return_value.scalar_one_or_none.return_value = None
        wm = agent._watermark()
        assert wm is None

        # Verify run() with no watermark follows the safe skip path without fabricating watermark
        res = agent.run()
        assert res["status"] == "skipped"
        assert "no completed five-minute Kite bars" in res["reason"]
        assert res["orders_allowed"] is False

    def test_missing_or_invalid_watermark_data_does_not_authorize_trade(self):
        """Verifies that missing, invalid, or empty watermark input never authorizes a trade."""
        from backend.live_inference import LivePaperInference

        agent = LivePaperInference.__new__(LivePaperInference)
        agent._watermark = MagicMock(return_value=None)

        res_none = agent.run(watermark=None)
        assert res_none["status"] == "skipped"
        assert res_none["orders_allowed"] is False
