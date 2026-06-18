"""
APEX TRADER — Dashboard Health Mixin (P1 — ops / production hardening)

Exposes the aggregate health snapshot produced by the ops layer
(:class:`ops.lifecycle.HealthCheck`) for the ``/api/health`` endpoint: broker
connectivity, every adaptive layer's active/dormant state, last-tick age, open
positions, drawdown state, process memory, on-disk store sizes, and the process
watchdog heartbeat.

Read-only — it never mutates the loop. Returns a stable shape even when the loop
is detached or the ops layer is disabled so the endpoint never 500s.
"""

from __future__ import annotations

from typing import Any

from loguru import logger


class HealthMixin:
    """get_health() — aggregate system health for /api/health."""

    def get_health(self) -> dict:
        ed = getattr(self, "_event_driven_system", None)
        if ed is not None:
            return self._ed_health()

        loop = getattr(self, "_trading_loop", None)
        if loop is None:
            return {
                "status": "ok",
                "ops_enabled": False,
                "attached": False,
                "running": False,
            }
        getter = getattr(loop, "get_ops_health", None)
        if not callable(getter):
            return {"status": "ok", "ops_enabled": False, "attached": True}
        try:
            snap = getter()
            snap.setdefault("attached", True)
            return snap
        except Exception as exc:  # noqa: BLE001
            logger.debug("[state_health] health read failed: {}", exc)
            return {"status": "error", "ops_enabled": True, "attached": True, "error": str(exc)}

    def _ed_health(self) -> dict:
        """Health snapshot for event-driven mode."""
        from datetime import datetime, timezone

        ed = self._event_driven_system
        ctx = getattr(self, "_system_context", None)
        pm = getattr(self, "_platform_manager", None)

        broker = {"any_connected": False, "mt5": False, "deriv": False}
        if pm is not None:
            try:
                broker["any_connected"] = bool(getattr(pm, "any_connected", False))
                flags = getattr(pm, "_mt5_connected_flags", []) or []
                broker["mt5"] = any(bool(f) for f in flags)
                deriv = getattr(pm, "deriv", None)
                broker["deriv"] = bool(deriv is not None and getattr(deriv, "is_connected", lambda: False)())
            except Exception:
                pass

        open_positions = 0
        try:
            positions = pm.get_all_open_positions() if pm else []
            open_positions = len(positions) if positions else 0
        except Exception:
            pass

        warnings: list[str] = []
        if not broker["any_connected"]:
            warnings.append("no broker connected")

        watchdog_state = None
        if ctx is not None and ctx.process_watchdog is not None:
            try:
                watchdog_state = ctx.process_watchdog.get_state()
                if watchdog_state.get("stalled"):
                    warnings.append("tick stall detected")
            except Exception:
                pass

        drawdown = {"source": "none"}
        if ctx is not None and ctx.drawdown_guard is not None:
            try:
                dd = ctx.drawdown_guard.get_status(datetime.now(timezone.utc))
                drawdown = {"source": "drawdown_guard", "mode": str(getattr(dd, "mode", "NORMAL"))}
            except Exception:
                pass

        status = "ok" if not warnings else "degraded"
        stats = ed.stats() if hasattr(ed, "stats") else {}

        return {
            "status": status,
            "ts": datetime.now(timezone.utc).isoformat(),
            "running": bool(getattr(ed, "is_running", False)),
            "mode": "event_driven",
            "broker": broker,
            "open_positions": open_positions,
            "drawdown": drawdown,
            "watchdog": watchdog_state,
            "ed_stats": stats,
            "warnings": warnings,
            "attached": True,
            "ops_enabled": True,
        }
