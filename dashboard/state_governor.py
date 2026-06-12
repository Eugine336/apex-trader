"""
APEX TRADER — Dashboard Portfolio Governor Mixin.

Exposes the live Portfolio Governor state: daily P&L vs the loss cap, whether
trading is halted, currency / sector exposure breakdowns, and the recent
governor blocks (with reasons).  Reads the governor + open positions straight
off the attached trading loop.
"""

from __future__ import annotations

from loguru import logger


class GovernorMixin:
    """get_governor() — portfolio-level risk state for the dashboard."""

    def get_governor(self) -> dict:
        if self.is_live:
            loop = self._trading_loop
            gov = getattr(loop, "_governor", None)
            if gov is not None:
                try:
                    snap = getattr(loop, "get_positions_snapshot", None)
                    positions = list(snap().values()) if snap else list(getattr(loop, "managed_positions", {}).values())
                    return gov.get_state(positions)
                except Exception as exc:
                    logger.debug("[dashboard] governor state read failed: {}", exc)

        return {
            "enabled": False,
            "trading_halted": False,
            "daily_pnl": 0.0,
            "daily_pnl_pct": 0.0,
            "daily_loss_cap_pct": 3.0,
            "daily_loss_recovery_pct": 1.5,
            "open_positions": 0,
            "max_open_positions": 8,
            "max_currency_exposure": 3,
            "max_sector_exposure": 4,
            "max_correlated_positions": 2,
            "currency_exposure": {},
            "sector_exposure": {},
            "recent_blocks": [],
        }
