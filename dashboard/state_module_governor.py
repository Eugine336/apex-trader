"""
APEX TRADER — Dashboard Module Governor Mixin (L3 — shadow mode)

The Module Governor (``adaptive.module_governor.ModuleGovernor``) keeps every
voting module in one of three modes — ACTIVE, SHADOW, or DISABLED — based on its
graded accuracy. A struggling module is moved to SHADOW (still running and
measured, but its vote weight forced to 0.0); it returns to ACTIVE if it
recovers or is fully DISABLED if it stays poor.

This mixin reads the governor off the live trading loop and exposes its
per-module status plus the transition audit trail for the Module Governor panel.
It only reads; it never makes or mutates a governance decision.
"""

from __future__ import annotations

from typing import Any

from loguru import logger


class ModuleGovernorMixin:
    """get_module_governor() — per-module modes + transition history."""

    def _module_governor(self) -> Any:
        ctx = getattr(self, "_system_context", None)
        if ctx is not None:
            gov = getattr(ctx, "module_governor", None)
            if gov is not None:
                return gov
        return None

    def get_module_governor(self, limit: int = 100) -> dict:
        """Per-module governance status + recent transitions for the dashboard."""
        gov = self._module_governor()
        if gov is None:
            return {
                "enabled": False,
                "wired": False,
                "modules": [],
                "counts": {"ACTIVE": 0, "SHADOW": 0, "DISABLED": 0},
                "module_count": 0,
                "transitions": [],
                "source": "idle",
            }
        try:
            status = gov.get_status() or {}
            transitions = gov.get_transitions(limit=int(limit)) or []
        except Exception as exc:  # noqa: BLE001
            logger.debug("[state_module_governor] read failed: {}", exc)
            return {
                "enabled": False,
                "wired": True,
                "modules": [],
                "counts": {"ACTIVE": 0, "SHADOW": 0, "DISABLED": 0},
                "module_count": 0,
                "transitions": [],
                "source": "error",
            }
        return {
            "enabled": bool(status.get("enabled", False)),
            "counterfactual_signal": bool(status.get("counterfactual_signal", False)),
            "wired": True,
            "modules": status.get("modules", []),
            "counts": status.get("counts", {}),
            "module_count": int(status.get("module_count", 0) or 0),
            "transitions": transitions,
            "source": "live",
        }
