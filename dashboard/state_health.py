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
        loop = getattr(self, "_trading_loop", None)
        if loop is None:
            # No loop attached (e.g. dashboard-only / startup) — report a stable
            # OK shape so liveness probes still succeed.
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
