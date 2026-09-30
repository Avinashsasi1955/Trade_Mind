"""Tick and Candle Sanitization Engine.

Eliminates live feed noise, bad ticks, out-of-order timestamp anomalies,
and enforces mathematical OHLCV invariants before persistence into PostgreSQL.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, Optional, Tuple

logger = logging.getLogger("nivesh.candle_sanitizer")


@dataclass
class InstrumentTickState:
    last_price: Optional[Decimal] = None
    last_timestamp: Optional[datetime] = None
    consecutive_outliers: int = 0
    pending_outlier_price: Optional[Decimal] = None
    last_raw_volume: int = 0


class TickSanitizer:
    """Real-time tick filter preventing freak spikes and stale ticks."""

    def __init__(
        self,
        equity_jump_threshold: float = 0.03,  # 3% sub-second jump threshold
        option_jump_threshold: float = 0.25,  # 25% jump threshold for options
        max_stale_seconds: float = 3.0,
    ):
        self.equity_jump_threshold = Decimal(str(equity_jump_threshold))
        self.option_jump_threshold = Decimal(str(option_jump_threshold))
        self.max_stale_seconds = timedelta(seconds=max_stale_seconds)
        self._states: Dict[int, InstrumentTickState] = {}
        self.rejected_ticks_count: int = 0

    def get_state(self, instrument_id: int) -> InstrumentTickState:
        if instrument_id not in self._states:
            self._states[instrument_id] = InstrumentTickState()
        return self._states[instrument_id]

    def validate_tick(
        self,
        instrument_id: int,
        price: Decimal,
        timestamp: datetime,
        volume: int,
        instrument_type: str = "EQ",
    ) -> Tuple[bool, Optional[str]]:
        """Validate if a tick is plausible. Returns (is_valid, rejection_reason)."""
        if price <= 0:
            self.rejected_ticks_count += 1
            return False, "NON_POSITIVE_PRICE"

        state = self.get_state(instrument_id)

        # 1. Monotonic Timestamp Verification
        if state.last_timestamp is not None:
            if timestamp < (state.last_timestamp - self.max_stale_seconds):
                self.rejected_ticks_count += 1
                return False, f"STALE_OUT_OF_ORDER: {timestamp} < {state.last_timestamp}"

        # 2. Dynamic Volatility Envelope / Freak Tick Filter
        if state.last_price is not None and state.last_price > 0:
            threshold = (
                self.option_jump_threshold
                if instrument_type in {"CE", "PE", "OPT"}
                else self.equity_jump_threshold
            )
            price_delta_pct = abs(price - state.last_price) / state.last_price

            if price_delta_pct > threshold:
                # If this is the first or second extreme jump, reject as a likely freak tick.
                # If 3 consecutive ticks confirm this new price level, accept it as real market movement.
                if state.consecutive_outliers < 2:
                    state.consecutive_outliers += 1
                    state.pending_outlier_price = price
                    self.rejected_ticks_count += 1
                    logger.warning(
                        "Rejected freak tick for instrument %d: price %s vs last %s (jump: %.2f%%)",
                        instrument_id,
                        price,
                        state.last_price,
                        float(price_delta_pct) * 100,
                    )
                    return False, f"FREAK_TICK_SPIKE_{float(price_delta_pct)*100:.1f}PCT"
                else:
                    # Confirmed by 3 consecutive ticks, reset outlier counter and accept
                    state.consecutive_outliers = 0
            else:
                state.consecutive_outliers = 0
                state.pending_outlier_price = None

        # Tick accepted: update instrument state
        state.last_price = price
        if state.last_timestamp is None or timestamp > state.last_timestamp:
            state.last_timestamp = timestamp
        state.last_raw_volume = max(state.last_raw_volume, volume)

        return True, None


class BarInvariantValidator:
    """Enforces mathematical OHLCV invariants before persistence."""

    @staticmethod
    def sanitize_bar(bar: Dict) -> Dict:
        """Ensures Low <= min(Open, Close) <= max(Open, Close) <= High, Low > 0, Volume >= 0."""
        open_p = Decimal(str(bar["open"]))
        close_p = Decimal(str(bar["close"]))
        high_p = Decimal(str(bar["high"]))
        low_p = Decimal(str(bar["low"]))

        # Assert & repair invariants
        min_oc = min(open_p, close_p)
        max_oc = max(open_p, close_p)

        sanitized_high = max(high_p, max_oc)
        sanitized_low = max(Decimal("0.05"), min(low_p, min_oc))

        first_vol = int(bar.get("first_volume") or 0)
        last_vol = int(bar.get("last_volume") or 0)
        volume = max(0, last_vol - first_vol)

        first_oi = int(bar.get("first_oi") or 0)
        last_oi = int(bar.get("last_oi") or 0)
        oi = last_oi if last_oi > 0 else None
        oi_change = (last_oi - first_oi) if oi is not None else None

        sanitized = dict(bar)
        sanitized["open"] = open_p
        sanitized["close"] = close_p
        sanitized["high"] = sanitized_high
        sanitized["low"] = sanitized_low
        sanitized["volume"] = volume
        sanitized["oi"] = oi
        sanitized["oi_change"] = oi_change

        return sanitized
