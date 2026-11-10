"""APEX TRADER — Dashboard Cross-Instrument Mixin.

Read-only status for the proactive observational watchlist. It never makes
or mutates a decision.
"""

from __future__ import annotations

from typing import Any

from loguru import logger


class CrossInstrumentMixin:
    """Expose proactive watchlist state."""

    def get_cross_instrument(self) -> dict:
        ed = getattr(self, "_event_driven_system", None)
        out: dict[str, Any] = {
            "available": False,
            "proactive_scanner": {},
            "watchlist": [],
        }

        try:
            scanner = getattr(ed, "_proactive_scanner", None) if ed is not None else None
            if scanner is not None:
                out["available"] = True
                out["proactive_scanner"] = scanner.stats()
                out["watchlist"] = scanner.get_watchlist()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[dash-xinst] scanner stats failed: {}", exc)

        return out


__all__ = ["CrossInstrumentMixin"]
