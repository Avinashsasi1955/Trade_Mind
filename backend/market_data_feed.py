"""Broker-independent Market Data Feed interface and Data Provenance Taxonomy (Phase 3).

Exposes:
1. DataProvenance enum: LIVE_STREAM, REST_BACKFILL, HISTORICAL_BHAVCOPY, CACHED, SYNTHETIC_FIXTURE.
2. classify_provenance(): Maps provider and legacy strings to canonical provenance.
3. MarketDataFeed(ABC): Shared abstract interface for market data feeds.
4. UpstoxMarketDataFeed: Adapter wrapping UpstoxStream without duplicate connections or auth.
5. validate_market_data_for_entry(): Fail-closed gate for new trade entries.
"""
from __future__ import annotations

import math
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, Optional, Tuple


class DataProvenance(str, Enum):
    """Canonical 5-tier market data provenance taxonomy (docs/TARGET_ARCHITECTURE.md §3.1)."""
    LIVE_STREAM = "LIVE_STREAM"
    REST_BACKFILL = "REST_BACKFILL"
    HISTORICAL_BHAVCOPY = "HISTORICAL_BHAVCOPY"
    CACHED = "CACHED"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"


_LIVE_STREAM_SOURCES = {
    "zerodha_kite",
    "upstox_v3",
    "live_stream",
    "upstox_live",
    "kite_live",
    "live_feed",
    "live",
}
LIVE_STREAM_SOURCES = frozenset(_LIVE_STREAM_SOURCES)

_REST_BACKFILL_SOURCES = {
    "upstox_rest_5m",
    "upstox_rest_intraday",
    "upstox_rest_history",
    "kite_gap_backfill",
    "gap_repair_finalizer",
    "rest_backfill",
    "upstox_rest_1s_proxy",
}

_HISTORICAL_BHAVCOPY_SOURCES = {
    "official_exchange_bhavcopy",
    "bhavcopy",
    "nse_bhavcopy",
    "bse_bhavcopy",
}

_CACHED_SOURCES = {
    "redis_tick",
    "cached",
    "nivesh:ticks:latest",
    "cache",
    "market",  # Redis price result default label
}

_SYNTHETIC_SOURCES = {
    "synthetic",
    "yahoo_sample",
    "market_snapshot_fallback",
    "simulated",
    "synthetic_fixture",
    "mock",
    "test_fixture",
    "analytical_black_scholes",
    "black_scholes",
    "analytical",
}

ENTRY_ELIGIBLE_PROVENANCES = frozenset({DataProvenance.LIVE_STREAM})


def classify_provenance(source: Optional[str]) -> DataProvenance:
    """Classify an arbitrary source label into the canonical 5-tier provenance taxonomy."""
    if not source or not isinstance(source, str):
        return DataProvenance.SYNTHETIC_FIXTURE
    s = source.strip().lower()
    if s in _LIVE_STREAM_SOURCES:
        return DataProvenance.LIVE_STREAM
    if s in _REST_BACKFILL_SOURCES:
        return DataProvenance.REST_BACKFILL
    if s in _HISTORICAL_BHAVCOPY_SOURCES:
        return DataProvenance.HISTORICAL_BHAVCOPY
    if s in _CACHED_SOURCES:
        return DataProvenance.CACHED
    if s in _SYNTHETIC_SOURCES:
        return DataProvenance.SYNTHETIC_FIXTURE

    # Fail closed to synthetic fixture for any unverified or unrecognized source.
    # An unknown source must never become trusted merely because it contains a familiar token.
    return DataProvenance.SYNTHETIC_FIXTURE


class MarketDataFeed(ABC):
    """Abstract interface for broker-independent market data feeds."""

    @abstractmethod
    def connect(self) -> None:
        """Start or initiate feed streaming."""
        pass

    @abstractmethod
    def subscribe(self, symbols: Iterable[str], mode: str = "ltpc") -> None:
        """Subscribe to quotes for the given symbol or key collection."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Safely terminate feed connections."""
        pass

    @property
    @abstractmethod
    def is_healthy(self) -> bool:
        """True only if feed is active, connected, and has received recent heartbeat."""
        pass

    @property
    @abstractmethod
    def provenance(self) -> DataProvenance:
        """Canonical data provenance for quotes delivered by this feed."""
        pass

    @abstractmethod
    def health_status(self) -> Dict[str, Any]:
        """Diagnostic state for logging and health checks."""
        pass


class UpstoxMarketDataFeed(MarketDataFeed):
    """Broker-independent adapter for Upstox V3 Market Data Feed."""

    def __init__(self, access_token: str = "", on_ticks=None, on_status=None):
        from .upstox_stream import UpstoxStream
        self._stream = UpstoxStream(access_token=access_token, on_ticks=on_ticks, on_status=on_status)

    def connect(self) -> None:
        self._stream.start()

    def subscribe(self, symbols: Iterable[str], mode: str = "ltpc") -> None:
        self._stream.subscribe(symbols, mode=mode)

    def stop(self) -> None:
        self._stream.stop()

    @property
    def is_healthy(self) -> bool:
        if not self._stream.configured or not self._stream.ws:
            return False
        if self._stream.last_heartbeat_at is None:
            return False
        # Heartbeat must be within 60 seconds
        return (time.time() - self._stream.last_heartbeat_at) < 60.0

    @property
    def provenance(self) -> DataProvenance:
        return DataProvenance.LIVE_STREAM

    def health_status(self) -> Dict[str, Any]:
        return {
            "provider": "upstox_v3",
            "configured": self._stream.configured,
            "connected": bool(self._stream.ws),
            "is_healthy": self.is_healthy,
            "reconnect_attempts": self._stream.reconnect_attempts,
            "message_count": self._stream.message_count,
            "decoded_tick_count": self._stream.decoded_tick_count,
            "decode_error_count": self._stream.decode_error_count,
            "last_heartbeat_at": self._stream.last_heartbeat_at,
            "provenance": self.provenance.value,
        }


def validate_quote_timestamp(
    observed_at: Optional[Any],
    max_age_seconds: Optional[float] = None,
    reference_time: Optional[Any] = None,
) -> Tuple[bool, Optional[float], Optional[str]]:
    """Validate quote observation timestamp for presence, valid format, future-dating, and freshness.

    Returns:
        (is_valid, age_seconds, error_reason)
    """
    if observed_at is None or (isinstance(observed_at, str) and not observed_at.strip()):
        return False, None, "PRICE_OBSERVED_AT_TIMESTAMP_MISSING"

    limit = (
        float(max_age_seconds)
        if max_age_seconds is not None
        else float(os.getenv("NIVESH_PRICE_MAX_AGE_SECONDS", "30.0"))
    )

    if isinstance(observed_at, datetime):
        dt = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=timezone.utc)
    elif isinstance(observed_at, (int, float)) and observed_at > 0:
        dt = datetime.fromtimestamp(observed_at, tz=timezone.utc)
    elif isinstance(observed_at, str) and observed_at.strip():
        try:
            dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            return False, None, f"INVALID_OBSERVED_AT_TIMESTAMP: {observed_at}"
    else:
        return False, None, f"INVALID_OBSERVED_AT_TIMESTAMP: {observed_at}"

    if reference_time is not None:
        if isinstance(reference_time, datetime):
            ref = reference_time if reference_time.tzinfo else reference_time.replace(tzinfo=timezone.utc)
        elif isinstance(reference_time, (int, float)) and reference_time > 0:
            ref = datetime.fromtimestamp(reference_time, tz=timezone.utc)
        elif isinstance(reference_time, str) and reference_time.strip():
            ref = datetime.fromisoformat(reference_time.replace("Z", "+00:00"))
            if not ref.tzinfo:
                ref = ref.replace(tzinfo=timezone.utc)
        else:
            ref = datetime.now(timezone.utc)
    else:
        ref = datetime.now(timezone.utc)

    age = (ref.astimezone(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
    if age < -5.0:
        return False, age, f"PRICE_TIMESTAMP_IN_FUTURE: age={age:.1f}s"
    if age > limit:
        return False, age, f"PRICE_STALE: age={age:.1f}s exceeds limit={limit:.1f}s"

    return True, age, None


def validate_market_data_for_entry(
    price: Any,
    observed_at: Optional[Any] = None,
    source: Optional[str] = None,
    max_age_seconds: Optional[float] = None,
    allow_synthetic: bool = False,
    feed_healthy: Optional[bool] = None,
    reference_time: Optional[Any] = None,
) -> Tuple[bool, Optional[str]]:
    """Fail-closed price validator for new paper-trade entry authorization.

    Ensures market data is:
    1. Present and non-null.
    2. Numeric, finite (not NaN, not Inf), and strictly > 0.
    3. From a healthy feed if feed status is provided.
    4. Has source metadata belonging to the exact traded instrument.
    5. Belongs to ENTRY_ELIGIBLE_PROVENANCES (LIVE_STREAM only, or explicit synthetic opt-in for isolated tests).
    6. Valid, non-blank, non-malformed, non-future, and fresh observation timestamp.
    """
    if price is None:
        return False, "PRICE_MISSING"

    try:
        val = float(price)
    except (TypeError, ValueError):
        return False, f"PRICE_NON_NUMERIC: {price}"

    if math.isnan(val) or math.isinf(val) or val <= 0:
        return False, f"PRICE_INVALID_NON_POSITIVE_OR_NON_FINITE: {val}"

    if feed_healthy is False:
        return False, "FEED_UNHEALTHY_OR_DISCONNECTED"

    if source is None or (isinstance(source, str) and not source.strip()):
        return False, "PRICE_SOURCE_METADATA_MISSING"

    prov = classify_provenance(source)
    if prov == DataProvenance.SYNTHETIC_FIXTURE:
        if not allow_synthetic:
            return False, f"SYNTHETIC_DATA_PROHIBITED_FOR_ENTRY (source: {source})"
    elif prov == DataProvenance.HISTORICAL_BHAVCOPY:
        return False, f"HISTORICAL_BHAVCOPY_CANNOT_MASQUERADE_AS_LIVE_ENTRY (source: {source})"
    elif prov == DataProvenance.REST_BACKFILL:
        return False, f"REST_BACKFILL_CANNOT_AUTHORIZE_ENTRY (source: {source})"
    elif prov == DataProvenance.CACHED:
        return False, f"CACHED_DATA_CANNOT_AUTHORIZE_ENTRY (source: {source})"
    elif prov not in ENTRY_ELIGIBLE_PROVENANCES:
        return False, f"PROVENANCE_NOT_ENTRY_ELIGIBLE: Provenance '{prov.value}' (source: '{source}') cannot authorize entries."

    is_valid_ts, _age, ts_err = validate_quote_timestamp(
        observed_at=observed_at,
        max_age_seconds=max_age_seconds,
        reference_time=reference_time,
    )
    if not is_valid_ts:
        return False, ts_err

    return True, None

