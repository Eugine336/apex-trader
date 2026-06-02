"""
APEX TRADER — Live State Manager
Bridges TradingLoop / PlatformManager to dashboard API responses.

Thin orchestrator — delegates to domain-specific mixins in
state_status, state_trades, state_history, state_scanner,
state_risk, state_performance, state_ml, and state_controls.
"""

import time as _time
from typing import Any

from loguru import logger

from dashboard.state_status import StatusMixin
from dashboard.state_trades import TradesMixin
from dashboard.state_history import HistoryMixin
from dashboard.state_scanner import ScannerMixin
from dashboard.state_risk import RiskMixin
from dashboard.state_performance import PerformanceMixin
from dashboard.state_ml import MLInsightsMixin
from dashboard.state_controls import ControlsMixin


class LiveState(
    StatusMixin,
    TradesMixin,
    HistoryMixin,
    ScannerMixin,
    RiskMixin,
    PerformanceMixin,
    MLInsightsMixin,
    ControlsMixin,
):
    """
    Central state provider for the dashboard.

    When a TradingLoop is attached (live mode), all data comes from the
    real engine. When nothing is attached, stable fallback shapes are
    returned so the frontend never breaks.
    """

    def __init__(self) -> None:
        self._trading_loop: Any = None
        self._platform_manager: Any = None
        self._start_time = _time.monotonic()
        self._running = False
        self._connection_status: dict[str, bool] = {}
        self._journal_cache: list[dict] = []
        self._journal_cache_ts: float = 0.0
        self._journal_cache_ttl: float = 5.0

    def attach(
        self,
        trading_loop: Any,
        platform_manager: Any,
        connection_status: dict[str, bool],
    ) -> None:
        self._trading_loop = trading_loop
        self._platform_manager = platform_manager
        self._connection_status = connection_status
        self._running = True
        logger.info("LiveState attached — dashboard serving real data")

    def get_activity(self) -> dict:
        """
        Returns merged activity feed for the dashboard:
        - Recent rejections (in-memory from TradingLoop + PlatformManager)
        - System warnings (broker errors, unavailable symbols, etc.)
        Sorted newest-first, capped at 100 entries.
        """
        events: list[dict] = []

        # Rejections + warnings from TradingLoop
        loop_warnings = getattr(self._trading_loop, "system_warnings", []) if self._trading_loop else []
        events.extend(loop_warnings)

        # Warnings from PlatformManager (broker errors, unavailable symbols)
        pm_warnings = getattr(self._platform_manager, "system_warnings", []) if self._platform_manager else []
        events.extend(pm_warnings)

        # Sort newest-first and cap
        try:
            events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        except Exception as exc:
            logger.debug("[dashboard] event sort failed: {}", exc)
            pass
        events = events[:100]

        return {
            "events": events,
            "total": len(events),
        }

    @property
    def is_live(self) -> bool:
        return self._trading_loop is not None and self._running
