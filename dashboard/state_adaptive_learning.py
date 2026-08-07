"""APEX TRADER — Dashboard Adaptive Learning Mixin (Phase 6).

Surfaces the continuous-learning closure: the bounded adaptive evidence weights
(vs their static defaults), the per-timeframe predictive accuracy that drives
them, the recommendation applier's recent bounded changes, and the adaptive
scheduler's run counts. Read-only — never makes or mutates a decision.
"""

from __future__ import annotations

from typing import Any

from loguru import logger


class AdaptiveLearningMixin:
    """get_adaptive_learning() — weights, accuracy, applier + scheduler state."""

    def get_adaptive_learning(self) -> dict:
        ctx = getattr(self, "_system_context", None)
        ed = getattr(self, "_event_driven_system", None)

        out: dict[str, Any] = {
            "available": False,
            "weights": {},
            "applier": {},
            "scheduler": {},
            "tuner": {},
        }

        try:
            awp = getattr(ctx, "adaptive_weight_provider", None) if ctx else None
            if awp is not None:
                out["available"] = True
                out["weights"] = awp.status()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[dash-adaptive] weights status failed: {}", exc)

        try:
            applier = getattr(ctx, "recommendation_applier", None) if ctx else None
            if applier is not None:
                out["available"] = True
                out["applier"] = applier.status()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[dash-adaptive] applier status failed: {}", exc)

        try:
            sched = getattr(ed, "_adaptive_scheduler", None) if ed is not None else None
            if sched is not None:
                out["available"] = True
                out["scheduler"] = sched.stats()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[dash-adaptive] scheduler stats failed: {}", exc)

        try:
            agent = getattr(ctx, "tuner_agent", None) if ctx else None
            if agent is not None:
                out["tuner"] = {
                    "enabled": bool(getattr(agent, "enabled", False)),
                    "registered": list(getattr(agent, "registered_names", []) or []),
                }
        except Exception as exc:  # noqa: BLE001
            logger.debug("[dash-adaptive] tuner status failed: {}", exc)

        return out


__all__ = ["AdaptiveLearningMixin"]
