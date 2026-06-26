"""APEX TRADER — Dashboard Cross-Instrument Mixin (GAP 1-5).

Surfaces the cross-instrument opportunity layer: the global opportunity queue
(collect → rank → dispatch), the cross-instrument ranking + quality sizing
config, the position displacer activity, and the proactive scanner's current
high-EV watchlist. Read-only — never makes or mutates a decision.
"""

from __future__ import annotations

from typing import Any

from loguru import logger


class CrossInstrumentMixin:
    """get_cross_instrument() — queue, sizer, displacer + proactive watchlist state."""

    def get_cross_instrument(self) -> dict:
        ctx = getattr(self, "_system_context", None)
        ed = getattr(self, "_event_driven_system", None)

        out: dict[str, Any] = {
            "available": False,
            "queue": {},
            "quality_sizer": {},
            "displacer": {},
            "proactive_scanner": {},
            "watchlist": [],
        }

        try:
            q = getattr(ed, "_opportunity_queue", None) if ed is not None else None
            if q is not None:
                out["available"] = True
                out["queue"] = q.stats()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[dash-xinst] queue stats failed: {}", exc)

        try:
            sizer = getattr(ctx, "opportunity_quality_sizer", None) if ctx else None
            if sizer is not None:
                out["available"] = True
                out["quality_sizer"] = sizer.status()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[dash-xinst] sizer status failed: {}", exc)

        try:
            disp = getattr(ctx, "position_displacer", None) if ctx else None
            if disp is not None:
                out["available"] = True
                out["displacer"] = disp.stats()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[dash-xinst] displacer stats failed: {}", exc)

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
