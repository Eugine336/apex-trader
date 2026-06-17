"""
APEX TRADER — Virtual Signal Manager (L5c shadow → promote → retire lifecycle)

The :class:`~adaptive.signal_discovery.SignalDiscoveryEngine` mines candidate
rules; the :class:`~adaptive.virtual_modules.VirtualModuleRegistry` stores them
as virtual voting modules and computes their votes.  This manager is the
*policy* that connects the two and closes the loop end-to-end:

1. **Ingest** — register newly-qualifying discovered rules as SHADOW virtual
   modules (they record + grade but never influence a decision).
2. **Promote** — when a shadow module has run long enough AND its graded
   accuracy clears the bar AND it is not harmful by marginal R, move it to
   ACTIVE with a real weight (subject to a hard active cap and a per-cycle
   promotion rate limit).
3. **Retire** — when an ACTIVE module degrades (accuracy below the retirement
   floor, or harmful marginal R), move it to DISABLED (weight 0.0).
4. **Restart-shadow tick** — clear the supervised restart window for modules
   that came back ACTIVE from disk once enough trades have elapsed.

It is **TunerAgent-guarded**: a direct call to :meth:`evaluate` while the agent
is sole authority is blocked, so every promotion/retirement flows through the
central tuner.  Inert unless ``virtual_promotion_enabled`` is on; reads accuracy
from the read-only EmitterFeedback service and marginal R from the read-only
CounterfactualEngine cache — it never grades signals or recomputes attribution
itself.  Exception-safe throughout: a fault here must never block trading.

Leaf-ish module — stdlib + loguru + the registry + ``TuningGuardMixin`` only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from adaptive.module_governor import ModuleMode
from adaptive.tunable import TuningGuardMixin
from adaptive.virtual_modules import (
    VirtualModuleRegistry,
    definition_from_rule,
)


@dataclass
class VirtualLifecycleResult:
    """Summary of one lifecycle evaluation pass."""

    registered: list[str] = field(default_factory=list)
    promoted: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    evaluated: int = 0
    reason: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.registered or self.promoted or self.retired)

    def to_dict(self) -> dict:
        return {
            "registered": list(self.registered),
            "promoted": list(self.promoted),
            "retired": list(self.retired),
            "evaluated": int(self.evaluated),
            "reason": self.reason,
            "changed": self.changed,
        }


class VirtualSignalManager(TuningGuardMixin):
    """Shadow → promote → retire policy for synthetic voting modules."""

    def __init__(
        self,
        registry: VirtualModuleRegistry,
        config,
        *,
        signal_discovery=None,
        emitter_feedback=None,
        counterfactual=None,
    ) -> None:
        self._registry = registry
        self._config = config
        self._signal_discovery = signal_discovery
        self._emitter_feedback = emitter_feedback
        self._counterfactual = counterfactual
        self._last_result: Optional[VirtualLifecycleResult] = None
        self._last_eval_ts: float = 0.0

    # ── Wiring ────────────────────────────────────────────────────────────

    def set_signal_discovery(self, engine) -> None:
        self._signal_discovery = engine

    def set_emitter_feedback(self, emitter_feedback) -> None:
        self._emitter_feedback = emitter_feedback

    def set_counterfactual(self, counterfactual) -> None:
        self._counterfactual = counterfactual

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._config, "virtual_promotion_enabled", False))

    @property
    def last_result(self) -> Optional[VirtualLifecycleResult]:
        return self._last_result

    # ── Core operation (guarded) ──────────────────────────────────────────

    def evaluate(self, total_trades: int) -> VirtualLifecycleResult:
        """Run one shadow → promote → retire pass.

        Guarded: a direct call while the TunerAgent is sole authority is blocked
        (returns an empty result) so promotions never bypass the central tuner.
        The agent's delegated call (via the tunable adapter) is authorised and
        proceeds. Never raises.
        """
        if self._tuning_blocked("evaluate"):
            return VirtualLifecycleResult(reason="tuning blocked (agent is sole authority)")
        return self._evaluate_unguarded(int(total_trades or 0))

    def _evaluate_unguarded(self, total_trades: int) -> VirtualLifecycleResult:
        result = VirtualLifecycleResult()
        # Keep the registry's kill switch in sync with the discovery flag so a
        # runtime flip of ``signal_discovery_enabled`` zeroes (or restores) every
        # virtual module's live weight on the next lifecycle pass.
        try:
            self._registry.set_enabled(
                bool(getattr(self._config, "signal_discovery_enabled", False))
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[virtual] kill-switch sync failed: {}", exc)
        # Always advance the restart-shadow window, even when promotion is off,
        # so restored ACTIVE modules eventually resume (when the feature is on).
        try:
            self._registry.tick_restart_shadow(total_trades)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[virtual] restart tick failed: {}", exc)

        if not self.enabled:
            result.reason = "virtual promotion disabled"
            self._last_result = result
            return result

        try:
            self._ingest_candidates(total_trades, result)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[virtual] ingest failed: {}", exc)
        try:
            self._promote_eligible(total_trades, result)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[virtual] promote failed: {}", exc)
        try:
            self._retire_degraded(total_trades, result)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[virtual] retire failed: {}", exc)

        self._last_result = result
        self._last_eval_ts = time.time()
        if result.changed:
            logger.info(
                "[virtual] lifecycle: registered={} promoted={} retired={}",
                result.registered, result.promoted, result.retired,
            )
        return result

    # ── Step 1: ingest discovered candidates ──────────────────────────────

    def _ingest_candidates(self, total_trades: int, result: VirtualLifecycleResult) -> None:
        engine = self._signal_discovery
        if engine is None or not getattr(engine, "enabled", False):
            return
        try:
            cached = engine.get_cached() or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("[virtual] discovery cache read failed: {}", exc)
            return
        rules = cached.get("rules", []) or []
        for rd in rules:
            # Only adopt rules the discovery engine flagged ACTIVE (qualifying +
            # within its own persistence-scored cap) — the strongest candidates.
            if not (rd.get("qualifies") and rd.get("active")):
                continue
            definition = definition_from_rule(rd)
            if definition is None:
                continue  # not a directional voter
            if self._registry.has(definition.name):
                continue
            if self._registry.register(
                definition, mode=ModuleMode.SHADOW, total_trades=total_trades,
                reason="discovered (shadow)",
            ):
                result.registered.append(definition.name)

    # ── Step 2: promote eligible shadows ──────────────────────────────────

    def _promote_eligible(self, total_trades: int, result: VirtualLifecycleResult) -> None:
        cfg = self._config
        shadow_trades_required = int(getattr(cfg, "shadow_trades_required", 50))
        min_accuracy = float(getattr(cfg, "min_shadow_accuracy", 0.52))
        min_marginal_r = float(getattr(cfg, "min_shadow_marginal_r", 0.0))
        min_signals = int(getattr(cfg, "promotion_min_signals", 20))
        max_per_cycle = max(0, int(getattr(cfg, "max_promotions_per_cycle", 1)))
        initial_weight = float(getattr(cfg, "promotion_initial_weight", 1.0))
        max_active = self._registry.max_active

        if max_per_cycle <= 0:
            return
        cf_map = self._marginal_r_map()
        promotions = 0
        for rec in self._registry.get_by_mode(ModuleMode.SHADOW):
            if promotions >= max_per_cycle:
                break
            if self._registry.active_count() >= max_active:
                break  # hard cap reached — no new promotions until one retires
            # Shadow must have run long enough since registration.
            if (total_trades - int(rec.created_trade)) < shadow_trades_required:
                continue
            accuracy, n = self._accuracy(rec.name)
            if n < min_signals or accuracy < min_accuracy:
                continue
            # Marginal R is only meaningful once active; for a shadow module it
            # is typically absent → treated as 0.0 (non-binding by default).
            cf = cf_map.get(rec.name)
            marginal_r = float(cf.get("mr_per_trade", 0.0)) if cf else 0.0
            if marginal_r < min_marginal_r:
                continue
            if self._registry.promote(
                rec.name, weight=initial_weight, total_trades=total_trades,
                reason=(
                    f"shadow accuracy {accuracy:.2f} >= {min_accuracy:.2f} over {n} signals"
                ),
                accuracy=accuracy, marginal_r=marginal_r, sample_size=n,
            ):
                result.promoted.append(rec.name)
                promotions += 1

    # ── Step 3: retire degraded actives ───────────────────────────────────

    def _retire_degraded(self, total_trades: int, result: VirtualLifecycleResult) -> None:
        cfg = self._config
        retire_accuracy = float(getattr(cfg, "retirement_accuracy_threshold", 0.48))
        retire_min_signals = int(getattr(cfg, "retirement_min_signals", 30))
        cf_min_trades = int(getattr(cfg, "retirement_marginal_r_min_trades", 50))
        cf_threshold = float(getattr(cfg, "retirement_marginal_r_threshold", -0.05))
        cf_map = self._marginal_r_map()
        for rec in self._registry.get_by_mode(ModuleMode.ACTIVE):
            accuracy, n = self._accuracy(rec.name)
            acc_bad = n >= retire_min_signals and accuracy < retire_accuracy
            cf = cf_map.get(rec.name)
            cf_bad = False
            marginal_r = 0.0
            if cf and int(cf.get("trades", 0)) >= cf_min_trades:
                marginal_r = float(cf.get("mr_per_trade", 0.0))
                cf_bad = bool(cf.get("better_off_without", False)) and marginal_r < cf_threshold
            if not (acc_bad or cf_bad):
                continue
            reasons: list[str] = []
            if acc_bad:
                reasons.append(f"accuracy {accuracy:.2f} < {retire_accuracy:.2f} over {n} signals")
            if cf_bad:
                reasons.append(f"marginal_R {marginal_r:+.3f} < {cf_threshold:+.3f}, better_off_without")
            if self._registry.disable(
                rec.name, reason="; ".join(reasons),
                accuracy=accuracy, marginal_r=marginal_r, sample_size=n,
            ):
                result.retired.append(rec.name)

    # ── Read helpers ──────────────────────────────────────────────────────

    def _accuracy(self, name: str) -> tuple[float, int]:
        """Graded accuracy + signal count for a virtual module (read-only)."""
        fb = self._emitter_feedback
        if fb is None:
            return 0.0, 0
        try:
            from adaptive.emitter_feedback import EmitterFeedbackRequest

            lookback = int(getattr(self._config, "feedback_lookback", 500))
            resp = fb.request_feedback(
                EmitterFeedbackRequest(emitter=name, lookback=lookback)
            )
            return (
                float(getattr(resp, "accuracy_all", 0.0) or 0.0),
                int(getattr(resp, "total_signals", 0) or 0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[virtual] accuracy read failed for {}: {}", name, exc)
            return 0.0, 0

    def _marginal_r_map(self) -> dict[str, dict]:
        """Per-module marginal R per trade from the counterfactual cache (read-only)."""
        cf = self._counterfactual
        if cf is None:
            return {}
        try:
            cached = cf.get_cached_attributions() or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("[virtual] cf cache read failed: {}", exc)
            return {}
        out: dict[str, dict] = {}
        for m in cached.get("modules", []) or []:
            try:
                module = str(m.get("module") or "")
                if not module:
                    continue
                trades = int(m.get("trades_involved", 0) or 0)
                marginal_r = float(m.get("marginal_r", 0.0) or 0.0)
                out[module] = {
                    "mr_per_trade": marginal_r / trades if trades > 0 else 0.0,
                    "trades": trades,
                    "better_off_without": bool(m.get("better_off_without", False)),
                    "marginal_r": marginal_r,
                }
            except Exception as exc:  # noqa: BLE001
                logger.debug("[virtual] cf row parse failed: {}", exc)
        return out

    # ── Dashboard ─────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        status = self._registry.get_status()
        status["promotion_enabled"] = self.enabled
        status["last_evaluation"] = (
            self._last_result.to_dict() if self._last_result else None
        )
        status["last_evaluation_ts"] = self._last_eval_ts or None
        return status


__all__ = ["VirtualSignalManager", "VirtualLifecycleResult"]
