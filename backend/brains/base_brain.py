"""Abstract base class for all Nivesh brains."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from .bus import MessageBus, get_bus

logger = logging.getLogger("nivesh.brain")


class BaseBrain(ABC):
    """
    Every Brain:
      - Has a name
      - Holds a reference to the shared MessageBus
      - Implements run() — called by the scheduler or directly
      - May publish events onto the bus after processing
    """

    name: str = "BaseBrain"

    def __init__(self, bus: Optional[MessageBus] = None):
        self.bus = bus or get_bus()
        self._register_handlers()
        logger.info("[%s] initialised and connected to bus", self.name)

    # ------------------------------------------------------------------
    # Override in subclass to subscribe to events
    # ------------------------------------------------------------------
    def _register_handlers(self) -> None:
        """Subscribe to bus events. Called once during __init__."""

    # ------------------------------------------------------------------
    # Primary entry point
    # ------------------------------------------------------------------
    @abstractmethod
    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute the brain's primary task.
        `context` carries inputs not available on the bus (e.g. db handle, user_id).
        Returns a result dict published via the bus or returned to the caller.
        """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _log(self, level: str, msg: str, **kw) -> None:
        getattr(logger, level)("[%s] %s %s", self.name, msg, kw or "")
