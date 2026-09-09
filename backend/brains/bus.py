"""
Nivesh Brains — typed in-process message bus.

Every Brain publishes typed Events onto the bus.  Other brains subscribe
to the event types they care about.  The bus is synchronous and in-process
(no Redis/Kafka needed by default) so it adds zero latency overhead and
works in the existing single-process server.  A Redis adapter can be
dropped in later by swapping BusBackend.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Type, TypeVar
from zoneinfo import ZoneInfo

logger = logging.getLogger("nivesh.bus")

IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Event base
# ---------------------------------------------------------------------------

@dataclass
class BusEvent:
    """All events on the bus inherit from this."""
    event_type: str = field(init=False)
    source_brain: str = ""
    ts: str = field(default_factory=lambda: datetime.now(IST).isoformat())
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.event_type = self.__class__.__name__

    def to_dict(self) -> Dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Concrete event types  (one per data-layer handoff)
# ---------------------------------------------------------------------------

@dataclass
class MarketDataReady(BusEvent):
    """MarketIntelBrain → ScreenerBrain, SignalBrain"""
    stocks: List[Dict] = field(default_factory=list)
    universe_count: int = 0

@dataclass
class ScreenerResultReady(BusEvent):
    """ScreenerBrain → SignalBrain"""
    candidates: List[Dict] = field(default_factory=list)
    screener_stats: Dict = field(default_factory=dict)

@dataclass
class SignalReady(BusEvent):
    """SignalBrain → RiskBrain, ExecutionBrain"""
    signals: List[Dict] = field(default_factory=list)        # serialised Signal dicts
    buy_count: int = 0
    sell_count: int = 0
    user_id: int = 0
    risk_profile: str = "balanced"

@dataclass
class SentimentReady(BusEvent):
    """SentimentBrain → SignalBrain (advisory), RiskBrain"""
    sentiment_summary: Dict = field(default_factory=dict)
    news_items: List[Dict] = field(default_factory=list)
    user_id: int = 0

@dataclass
class RiskVerdictReady(BusEvent):
    """RiskBrain → ExecutionBrain"""
    approved_signals: List[Dict] = field(default_factory=list)
    rejected_signals: List[Dict] = field(default_factory=list)
    risk_summary: Dict = field(default_factory=dict)
    user_id: int = 0

@dataclass
class TradeExecuted(BusEvent):
    """ExecutionBrain → ReportBrain, PositionMonitorBrain"""
    trade: Optional[Dict] = None
    user_id: int = 0
    direction: str = ""   # "BUY" | "SELL"

@dataclass
class PositionAlert(BusEvent):
    """PositionMonitorBrain → ExecutionBrain, ReportBrain"""
    symbol: str = ""
    alert_type: str = ""   # "STOP_HIT" | "TARGET_HIT" | "RISK_BREACH"
    current_price: float = 0.0
    entry_price: float = 0.0
    pnl: float = 0.0
    user_id: int = 0

@dataclass
class DailyReportReady(BusEvent):
    """ReportBrain → (future: notification brain / Telegram)"""
    report: Dict = field(default_factory=dict)
    user_id: int = 0

@dataclass
class CandidateRejected(BusEvent):
    """LivePaperInference → Operations/Risk/Report advisory memory"""
    symbol: str = ""
    reason: str = ""
    strategy: str = ""
    probability: float = 0.0
    quality_score: float = 0.0

@dataclass
class SeniorApproved(BusEvent):
    """Senior layer → shadow ledger / Operations advisory memory"""
    symbol: str = ""
    side: str = ""
    strategy: str = ""
    confidence: float = 0.0
    route: str = ""

@dataclass
class ShadowTradeOpened(BusEvent):
    """LivePaperInference → Operations/Report durable event"""
    audit_id: int = 0
    symbol: str = ""
    side: str = ""
    quantity: int = 0
    route: str = ""

@dataclass
class ShadowTradeClosed(BusEvent):
    """Validation/manager → Operations/Report durable event"""
    audit_id: int = 0
    symbol: str = ""
    net_pnl: float = 0.0
    exit_reason: str = ""

@dataclass
class GapDetected(BusEvent):
    """Stream service → Operations durable event"""
    provider: str = ""
    stream_id: str = ""
    affected_tokens: int = 0

@dataclass
class GapRepaired(BusEvent):
    """Gap repair task/stream reconnect → Operations durable event"""
    provider: str = ""
    repaired_bars: int = 0
    remaining_open_gaps: int = 0


# ---------------------------------------------------------------------------
# Bus implementation
# ---------------------------------------------------------------------------

E = TypeVar("E", bound=BusEvent)
Handler = Callable[[BusEvent], None]


class MessageBus:
    """Thread-safe in-process pub/sub bus."""

    def __init__(self):
        self._lock = threading.Lock()
        self._handlers: Dict[str, List[Handler]] = defaultdict(list)
        self._history: List[BusEvent] = []
        self._max_history = 500

    def subscribe(self, event_cls: Type[E], handler: Handler) -> None:
        with self._lock:
            self._handlers[event_cls.__name__].append(handler)
        logger.debug("Subscribed %s → %s", event_cls.__name__, handler)

    def publish(self, event: BusEvent) -> None:
        with self._lock:
            handlers = list(self._handlers.get(event.event_type, []))
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history.pop(0)
        if not handlers:
            logger.debug("No handlers for %s", event.event_type)
            return
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:
                logger.exception("Handler %s raised for %s: %s", handler, event.event_type, exc)

    def recent(self, event_cls: Optional[Type[E]] = None, n: int = 50) -> List[BusEvent]:
        with self._lock:
            items = self._history if event_cls is None else [e for e in self._history if e.event_type == event_cls.__name__]
        return items[-n:]


# ---------------------------------------------------------------------------
# Global singleton bus
# ---------------------------------------------------------------------------

_bus: Optional[MessageBus] = None
_bus_lock = threading.Lock()


def get_bus() -> MessageBus:
    global _bus
    if _bus is None:
        with _bus_lock:
            if _bus is None:
                _bus = MessageBus()
                logger.info("MessageBus initialised")
    return _bus
