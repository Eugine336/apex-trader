"""APEX TRADER — Recommendation Applier (Phase 6, Department ⑦ → live config).

The :class:`~adaptive.recommendations.RecommendationGateway` records and
Governance authorises learner recommendations, but historically nothing applied
the approved ``PARAM_PROMOTE`` recommendations — the promote callback always
returned ``False`` (shadow only), so proven parameter candidates never reached
the live config. This applier closes that gap **within hard, reversible
bounds**:

* Only parameters explicitly **registered** as safe targets can be changed.
* Every change is clamped to ``±max_change_pct`` of the current value AND to the
  target's own absolute ``[min, max]`` band.
* Every applied change is logged with before/after and pushed onto a bounded
  audit ring for the dashboard.
* :meth:`reset_target` / :meth:`reset_all` revert a target to the value it had
  when first registered (the known-good baseline) — an instant escape hatch.

Wired as the gateway's applier (``gateway.set_applier(applier.apply)``) so it
fires only on an APPROVED recommendation. Leaf-ish — stdlib + loguru only.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional

from loguru import logger

from adaptive.recommendations import LearningRecommendation, RecommendationType


class _Target:
    __slots__ = ("name", "getter", "setter", "lo", "hi", "baseline")

    def __init__(
        self,
        name: str,
        getter: Callable[[], float],
        setter: Callable[[float], None],
        lo: float,
        hi: float,
    ) -> None:
        self.name = name
        self.getter = getter
        self.setter = setter
        self.lo = float(lo)
        self.hi = float(hi)
        try:
            self.baseline = float(getter())
        except Exception:  # noqa: BLE001
            self.baseline = float(lo)


class RecommendationApplier:
    """Applies APPROVED PARAM_PROMOTE recommendations to registered targets."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        max_change_pct: float = 0.20,
        history_limit: int = 200,
    ) -> None:
        self.enabled = bool(enabled)
        self._max_change_pct = abs(float(max_change_pct))
        self._targets: Dict[str, _Target] = {}
        self._history: Deque[dict] = deque(maxlen=max(1, int(history_limit)))
        self._applied_count = 0
        self._lock = threading.RLock()

    # ── Registration ─────────────────────────────────────────────────────

    def register_target(
        self,
        name: str,
        getter: Callable[[], float],
        setter: Callable[[float], None],
        *,
        lo: float,
        hi: float,
    ) -> None:
        """Register a config parameter as a safe, bounded apply target."""
        if not name:
            return
        with self._lock:
            self._targets[name] = _Target(name, getter, setter, lo, hi)

    @property
    def target_names(self) -> list[str]:
        with self._lock:
            return list(self._targets.keys())

    # ── Apply (gateway hook) ─────────────────────────────────────────────

    def apply(self, rec: LearningRecommendation) -> None:
        """Apply one APPROVED recommendation. Never raises."""
        try:
            if not self.enabled:
                return
            if rec.recommendation_type != RecommendationType.PARAM_PROMOTE:
                return  # only parameter promotions are applied here
            payload = rec.payload or {}
            name = str(payload.get("param_name", "") or "")
            proposed = payload.get("proposed_value", None)
            if not name or proposed is None:
                return
            with self._lock:
                target = self._targets.get(name)
                if target is None:
                    logger.debug(
                        "[rec-applier] no registered target for '{}' — skipped", name,
                    )
                    return
                try:
                    current = float(target.getter())
                except Exception:  # noqa: BLE001
                    current = target.baseline
                bounded = self._bound(current, float(proposed), target)
                if abs(bounded - current) <= 1e-12:
                    return  # already there / change clamped to nothing
                try:
                    target.setter(bounded)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[rec-applier] setter for '{}' failed: {}", name, exc)
                    return
                self._applied_count += 1
                entry = {
                    "timestamp": time.time(),
                    "source": rec.source,
                    "param_name": name,
                    "before": round(current, 6),
                    "proposed": round(float(proposed), 6),
                    "after": round(bounded, 6),
                }
                self._history.append(entry)
            logger.info(
                "[rec-applier] applied {} from {}: {} → {} (proposed {})",
                name, rec.source, round(current, 6), round(bounded, 6),
                round(float(proposed), 6),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[rec-applier] apply fell back: {}", exc)

    def _bound(self, current: float, proposed: float, target: _Target) -> float:
        """Clamp ``proposed`` to ±max_change_pct of current and the abs band."""
        if current != 0.0:
            lo_step = current * (1.0 - self._max_change_pct)
            hi_step = current * (1.0 + self._max_change_pct)
            step_lo, step_hi = min(lo_step, hi_step), max(lo_step, hi_step)
            proposed = min(step_hi, max(step_lo, proposed))
        return min(target.hi, max(target.lo, proposed))

    # ── Reversibility / observability ────────────────────────────────────

    def reset_target(self, name: str) -> bool:
        with self._lock:
            target = self._targets.get(name)
            if target is None:
                return False
            try:
                target.setter(target.baseline)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[rec-applier] reset '{}' failed: {}", name, exc)
                return False
        logger.warning("[rec-applier] '{}' reset to baseline {}", name, target.baseline)
        return True

    def reset_all(self) -> None:
        with self._lock:
            names = list(self._targets.keys())
        for name in names:
            self.reset_target(name)

    def status(self) -> dict:
        with self._lock:
            targets = {}
            for name, t in self._targets.items():
                try:
                    cur = round(float(t.getter()), 6)
                except Exception:  # noqa: BLE001
                    cur = None
                targets[name] = {
                    "current": cur,
                    "baseline": round(t.baseline, 6),
                    "min": t.lo,
                    "max": t.hi,
                }
            return {
                "enabled": self.enabled,
                "max_change_pct": self._max_change_pct,
                "applied_count": self._applied_count,
                "targets": targets,
                "recent": list(self._history)[-50:],
            }


__all__ = ["RecommendationApplier"]
