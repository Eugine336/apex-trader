"""APEX TRADER — Governance Division (Department ⑧).

Governance is the only layer allowed to turn a *recommendation* into a change
that affects live behaviour.  The Learning Division (⑦) measures outcomes and
*recommends*; Governance *authorises*.  Nothing the learners produce mutates the
live path until it has passed through here.

Responsibilities (and only these):

* **Authorise Learning recommendations.**  :meth:`authorize` is the authoriser
  wired into :class:`adaptive.recommendations.RecommendationGateway`.  Every
  WEIGHT_UPDATE / SIZE_ADJUST / AVOID_PATTERN / PROFILE_CHANGE / MODULE_SUPPRESS
  / TOXIC_PAIR_BLOCK passes through it and is either AUTHORIZED or REJECTED.
* **Control the module-promotion lifecycle.**  :meth:`authorize_promotion`
  enforces ``SHADOW → VALIDATION → LIMITED → FULL`` — no module (real or virtual)
  reaches full authority without Governance sign-off at each rung.
* **Consume InteractionAnalyzer findings.**  Toxic module-pair recommendations
  are tracked, and (when enforcement is enabled) the weaker module of a toxic
  pair is contained.
* **Issue containment via the enforcement arm.**  Governance never tunes; it
  *orders* the TunerAgent to freeze a runaway tunable, and orders the
  ModuleGovernor to shadow a harmful module.
* **Audit everything.**  Every decision is appended to bounded ring buffers for
  the dashboard / ops to inspect.

Design principles:

* **Fail-closed.**  An unknown recommendation type, or any error while
  evaluating one, yields REJECTED — a Governance gate that silently approves on
  a bug cannot be trusted.
* **Behaviour-neutral on deploy.**  The default authorisation criteria are
  permissive-but-bounded: every recommendation the existing learners actually
  emit is AUTHORIZED, so flipping governance on changes *nothing* about how the
  system trades — it only adds the explicit gate (and rejects pathological
  values that the learners never produce).
* **Never generates its own recommendations.**  Governance only authorises or
  rejects what Learning proposes.

Leaf module — standard library + loguru + the governance models/verdict only.
It never imports the live loop or a broker.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from loguru import logger

from governance.models import (
    AuthorizationRecord,
    PromotionRecord,
    PromotionStage,
    ToxicPairRecord,
    _pair_key,
)
from governance.verdict import GovernanceVerdict

# Recommendation type constants (kept as literals so this module does not import
# the Learning layer — it only authorises objects that carry these strings).
_WEIGHT_UPDATE = "WEIGHT_UPDATE"
_AVOID_PATTERN = "AVOID_PATTERN"
_SIZE_ADJUST = "SIZE_ADJUST"
_PROFILE_CHANGE = "PROFILE_CHANGE"
_MODULE_SUPPRESS = "MODULE_SUPPRESS"
_TOXIC_PAIR_BLOCK = "TOXIC_PAIR_BLOCK"

# The recommendation types Governance knows how to evaluate.  Anything else is
# fail-closed REJECTED.
_KNOWN_TYPES = frozenset(
    {
        _WEIGHT_UPDATE,
        _AVOID_PATTERN,
        _SIZE_ADJUST,
        _PROFILE_CHANGE,
        _MODULE_SUPPRESS,
        _TOXIC_PAIR_BLOCK,
    }
)


class GovernanceDivision:
    """Authorisation + containment authority over the Learning Division."""

    def __init__(
        self,
        *,
        module_governor: Optional[object] = None,
        tuner_agent: Optional[object] = None,
        virtual_registry: Optional[object] = None,
        # Bounds — wide enough to authorise everything the learners emit today
        # (behaviour-neutral) while still rejecting pathological values.
        min_size_multiplier: float = 0.0,
        max_size_multiplier: float = 5.0,
        max_weight_multiplier: float = 10.0,
        # Toxic-pair enforcement is OFF by default (behaviour-neutral): the
        # finding is always recorded, but Governance only contains a module when
        # explicitly enabled.
        enforce_toxic_pairs: bool = False,
        # Module-promotion lifecycle thresholds (per rung).
        validation_min_signals: int = 20,
        validation_min_accuracy: float = 0.50,
        limited_min_signals: int = 40,
        limited_min_accuracy: float = 0.52,
        full_min_signals: int = 80,
        full_min_accuracy: float = 0.55,
        full_min_marginal_r: float = 0.0,
        history_limit: int = 500,
    ) -> None:
        self._module_governor = module_governor
        self._tuner_agent = tuner_agent
        self._virtual_registry = virtual_registry

        self._min_size_mult = float(min_size_multiplier)
        self._max_size_mult = float(max_size_multiplier)
        self._max_weight_mult = float(max_weight_multiplier)
        self._enforce_toxic = bool(enforce_toxic_pairs)

        self._validation_min_signals = int(validation_min_signals)
        self._validation_min_accuracy = float(validation_min_accuracy)
        self._limited_min_signals = int(limited_min_signals)
        self._limited_min_accuracy = float(limited_min_accuracy)
        self._full_min_signals = int(full_min_signals)
        self._full_min_accuracy = float(full_min_accuracy)
        self._full_min_marginal_r = float(full_min_marginal_r)

        limit = max(1, int(history_limit))
        self._auth_log: Deque[AuthorizationRecord] = deque(maxlen=limit)
        self._promotion_log: Deque[PromotionRecord] = deque(maxlen=limit)
        self._toxic_pairs: Dict[Tuple[str, str], ToxicPairRecord] = {}
        self._counts: Dict[str, int] = {
            GovernanceVerdict.AUTHORIZED.value: 0,
            GovernanceVerdict.REJECTED.value: 0,
            GovernanceVerdict.DEFERRED.value: 0,
        }
        self._lock = threading.Lock()

    # ── Wiring (injected after construction) ──────────────────────────────

    def bind_runtime(
        self,
        *,
        module_governor: Optional[object] = None,
        tuner_agent: Optional[object] = None,
        virtual_registry: Optional[object] = None,
    ) -> None:
        """Inject the enforcement-arm references after construction.

        Only non-None arguments overwrite existing wiring, so partial binding is
        safe (mirrors ``ComplianceDivision.bind_runtime``)."""
        if module_governor is not None:
            self._module_governor = module_governor
        if tuner_agent is not None:
            self._tuner_agent = tuner_agent
        if virtual_registry is not None:
            self._virtual_registry = virtual_registry

    # ── Learning recommendation authorisation (the ⑦→⑧ boundary) ──────────

    def authorize(self, recommendation: Any) -> Tuple[bool, str]:
        """Authorise (or reject) one Learning recommendation.

        This is the callable wired into the :class:`RecommendationGateway`.
        Returns ``(approved, reason)``.  Fail-closed: an unknown type or any
        evaluation error rejects.  Every decision is audited.
        """
        rec_type = str(getattr(recommendation, "recommendation_type", "") or "")
        source = str(getattr(recommendation, "source", "") or "unknown")
        payload = _as_dict(getattr(recommendation, "payload", None))
        confidence = _as_float(getattr(recommendation, "confidence", 0.0))

        try:
            verdict, reason = self._evaluate(rec_type, payload)
        except Exception as exc:  # noqa: BLE001 — fail-closed on any fault
            verdict, reason = GovernanceVerdict.REJECTED, f"evaluation error: {exc}"
            logger.warning(
                "[governance] authorisation errored on {} from {} — REJECTED: {}",
                rec_type or "?", source, exc,
            )

        self._record_authorization(source, rec_type, verdict, reason, confidence, payload)
        if not verdict.approved:
            logger.info(
                "[governance] {} from {} {} — {}",
                rec_type or "?", source, verdict.value, reason,
            )
        return verdict.approved, reason

    def _evaluate(self, rec_type: str, payload: dict) -> Tuple[GovernanceVerdict, str]:
        if rec_type not in _KNOWN_TYPES:
            return GovernanceVerdict.REJECTED, f"unknown recommendation type '{rec_type}'"

        if rec_type == _SIZE_ADJUST:
            return self._eval_size_adjust(payload)
        if rec_type == _WEIGHT_UPDATE:
            return self._eval_weight_update(payload)
        if rec_type == _TOXIC_PAIR_BLOCK:
            return self._eval_toxic_pair(payload)
        # AVOID_PATTERN, MODULE_SUPPRESS, PROFILE_CHANGE are protective or
        # low-risk selections the learners already emit — authorise them
        # (behaviour-neutral) once the type is recognised.
        if rec_type == _PROFILE_CHANGE:
            return self._eval_profile_change(payload)
        return GovernanceVerdict.AUTHORIZED, f"{rec_type} authorised"

    def _eval_size_adjust(self, payload: dict) -> Tuple[GovernanceVerdict, str]:
        """A position-size multiplier is authorised only within sane bounds."""
        mult = payload.get("multiplier")
        if mult is None:
            # No bound to check — authorise (behaviour-neutral for a malformed
            # but recognised recommendation).
            return GovernanceVerdict.AUTHORIZED, "size adjust (no multiplier to bound)"
        m = _as_float(mult, default=None)
        if m is None or not math.isfinite(m):
            return GovernanceVerdict.REJECTED, f"non-finite size multiplier {mult!r}"
        if m < self._min_size_mult or m > self._max_size_mult:
            return (
                GovernanceVerdict.REJECTED,
                f"size multiplier {m:.3f} outside "
                f"[{self._min_size_mult:.2f}, {self._max_size_mult:.2f}]",
            )
        return GovernanceVerdict.AUTHORIZED, f"size multiplier {m:.3f} within bounds"

    def _eval_weight_update(self, payload: dict) -> Tuple[GovernanceVerdict, str]:
        """A vote-weight multiplier map is authorised when every entry is a
        finite, non-negative value within the configured ceiling."""
        mults = payload.get("multipliers")
        if not isinstance(mults, dict) or not mults:
            return GovernanceVerdict.AUTHORIZED, "weight update (empty/neutral map)"
        for module, value in mults.items():
            v = _as_float(value, default=None)
            if v is None or not math.isfinite(v):
                return GovernanceVerdict.REJECTED, f"non-finite weight for {module}"
            if v < 0.0 or v > self._max_weight_mult:
                return (
                    GovernanceVerdict.REJECTED,
                    f"weight {v:.3f} for {module} outside "
                    f"[0, {self._max_weight_mult:.2f}]",
                )
        return GovernanceVerdict.AUTHORIZED, f"{len(mults)} weight(s) within bounds"

    def _eval_profile_change(self, payload: dict) -> Tuple[GovernanceVerdict, str]:
        profile = payload.get("profile")
        if profile is None or str(profile).strip() == "":
            return GovernanceVerdict.REJECTED, "profile change with no profile"
        return GovernanceVerdict.AUTHORIZED, f"profile '{profile}' authorised"

    def _eval_toxic_pair(self, payload: dict) -> Tuple[GovernanceVerdict, str]:
        """Record a toxic module-pair finding and (optionally) contain it.

        The finding is always AUTHORIZED (Governance accepts the report and now
        tracks it).  Enforcement — shadowing the weaker module — only happens
        when ``enforce_toxic_pairs`` is enabled and a ModuleGovernor is wired.
        """
        module_a = str(payload.get("module_a") or "")
        module_b = str(payload.get("module_b") or "")
        if not module_a or not module_b:
            return GovernanceVerdict.REJECTED, "toxic pair missing a module name"
        effect = _as_float(payload.get("interaction_effect", 0.0))
        enforced, enforced_module = self._track_toxic_pair(module_a, module_b, effect)
        if enforced:
            return (
                GovernanceVerdict.AUTHORIZED,
                f"toxic pair {module_a}+{module_b} recorded; contained {enforced_module}",
            )
        return (
            GovernanceVerdict.AUTHORIZED,
            f"toxic pair {module_a}+{module_b} recorded (effect {effect:+.3f})",
        )

    def _track_toxic_pair(
        self, module_a: str, module_b: str, effect: float,
    ) -> Tuple[bool, str]:
        """Upsert the toxic-pair record; contain the weaker module when enabled.

        Returns ``(enforced, enforced_module)``.  Containment shadows the
        module with the worse standalone marginal-R (read from the
        ModuleGovernor's counterfactual signal) — or, absent that signal,
        ``module_b`` — and is only attempted once per pair.
        """
        key = _pair_key(module_a, module_b)
        with self._lock:
            rec = self._toxic_pairs.get(key)
            if rec is None:
                rec = ToxicPairRecord(
                    module_a=key[0], module_b=key[1], interaction_effect=effect,
                )
                self._toxic_pairs[key] = rec
            else:
                rec.occurrences += 1
                rec.interaction_effect = effect
                import time as _t
                rec.last_seen = _t.time()
            already_enforced = rec.enforced

        if not self._enforce_toxic or already_enforced:
            return False, ""
        target = self._weaker_module(module_a, module_b)
        contained = self.contain_module(
            target, reason=f"toxic pair with {module_a if target == module_b else module_b}",
        )
        if contained:
            with self._lock:
                rec = self._toxic_pairs.get(key)
                if rec is not None:
                    rec.enforced = True
                    rec.enforced_module = target
            return True, target
        return False, ""

    def _weaker_module(self, module_a: str, module_b: str) -> str:
        """Pick the module to contain in a toxic pair — the one with the worse
        standalone marginal R (per the governor's read-only counterfactual
        signal).  Falls back to ``module_b`` when no signal is available."""
        gov = self._module_governor
        getter = getattr(gov, "_cf_signal_map", None) if gov is not None else None
        if not callable(getter):
            return module_b
        try:
            cf = getter() or {}
        except Exception:  # noqa: BLE001
            return module_b
        mr_a = float((cf.get(module_a) or {}).get("mr_per_trade", 0.0) or 0.0)
        mr_b = float((cf.get(module_b) or {}).get("mr_per_trade", 0.0) or 0.0)
        return module_a if mr_a < mr_b else module_b

    # ── Module-promotion lifecycle (SHADOW → VALIDATION → LIMITED → FULL) ──

    def authorize_promotion(
        self,
        module: str,
        current_stage: PromotionStage | str,
        metrics: Optional[dict] = None,
    ) -> GovernanceVerdict:
        """Authorise a single lifecycle step for ``module``.

        Returns ``AUTHORIZED`` to advance one rung, ``DEFERRED`` when the
        candidate has not yet earned the next rung (ask again with more
        evidence), or ``REJECTED`` when promotion is denied (already at full, or
        the evidence is actively harmful).  Records the decision either way.
        """
        stage = PromotionStage.coerce(current_stage)
        m = metrics or {}
        target = stage.next_stage()
        if target is None:
            self._record_promotion(module, stage, stage, GovernanceVerdict.DEFERRED,
                                    "already at FULL authority", m)
            return GovernanceVerdict.DEFERRED

        verdict, reason = self._evaluate_promotion(target, m)
        self._record_promotion(
            module, stage, target if verdict.approved else stage, verdict, reason, m,
        )
        if verdict.approved:
            logger.info(
                "[governance] promotion AUTHORIZED — {} {}→{} ({})",
                module, stage.value, target.value, reason,
            )
        return verdict

    def _evaluate_promotion(
        self, target: PromotionStage, metrics: dict,
    ) -> Tuple[GovernanceVerdict, str]:
        accuracy = _as_float(metrics.get("accuracy", 0.0))
        signals = int(_as_float(metrics.get("sample_size", 0)))
        marginal_r = _as_float(metrics.get("marginal_r", 0.0))
        better_off_without = bool(metrics.get("better_off_without", False))

        # Actively harmful evidence denies any advance outright.
        if better_off_without and marginal_r < 0.0:
            return (
                GovernanceVerdict.REJECTED,
                f"harmful by attribution (marginal_R={marginal_r:+.3f}, "
                "better_off_without)",
            )

        if target == PromotionStage.VALIDATION:
            need_n, need_acc = self._validation_min_signals, self._validation_min_accuracy
        elif target == PromotionStage.LIMITED:
            need_n, need_acc = self._limited_min_signals, self._limited_min_accuracy
        else:  # FULL
            need_n, need_acc = self._full_min_signals, self._full_min_accuracy
            if marginal_r < self._full_min_marginal_r:
                return (
                    GovernanceVerdict.DEFERRED,
                    f"marginal_R {marginal_r:+.3f} < {self._full_min_marginal_r:+.3f} "
                    "for FULL authority",
                )

        if signals < need_n:
            return (
                GovernanceVerdict.DEFERRED,
                f"{signals} graded signals < {need_n} required for {target.value}",
            )
        if accuracy < need_acc:
            return (
                GovernanceVerdict.DEFERRED,
                f"accuracy {accuracy:.2f} < {need_acc:.2f} required for {target.value}",
            )
        return (
            GovernanceVerdict.AUTHORIZED,
            f"accuracy {accuracy:.2f} over {signals} signals clears {target.value}",
        )

    # ── Enforcement arm (Governance orders; others execute) ────────────────

    def contain_module(self, module: str, *, reason: str = "") -> bool:
        """Order the ModuleGovernor to shadow a harmful module.

        Governance decides; the ModuleGovernor executes (it owns the mode
        machine + audit).  No-op (returns False) when no governor is wired."""
        gov = self._module_governor
        force = getattr(gov, "force_mode", None) if gov is not None else None
        if not callable(force):
            return False
        try:
            from adaptive.module_governor import ModuleMode

            ok = bool(force(module, ModuleMode.SHADOW, reason=f"governance: {reason}"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[governance] contain_module({}) failed: {}", module, exc)
            return False
        if ok:
            logger.info("[governance] CONTAINED {} → SHADOW ({})", module, reason)
        return ok

    def freeze_tuning(self, tunable_name: str, *, reason: str = "") -> bool:
        """Order the TunerAgent to freeze a runaway tunable.

        Governance decides; the TunerAgent executes + audits.  No-op (False)
        when no agent is wired or it lacks the enforcement hook."""
        agent = self._tuner_agent
        freeze = getattr(agent, "freeze_tunable", None) if agent is not None else None
        if not callable(freeze):
            return False
        try:
            return bool(freeze(tunable_name, reason=f"governance: {reason}"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[governance] freeze_tuning({}) failed: {}", tunable_name, exc)
            return False

    def release_tuning(self, tunable_name: str) -> bool:
        """Order the TunerAgent to re-enable a previously-frozen tunable."""
        agent = self._tuner_agent
        reset = getattr(agent, "reset_failure_count", None) if agent is not None else None
        if not callable(reset):
            return False
        try:
            return bool(reset(tunable_name))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[governance] release_tuning({}) failed: {}", tunable_name, exc)
            return False

    # ── Audit / dashboard surface ──────────────────────────────────────────

    def _record_authorization(
        self, source: str, rec_type: str, verdict: GovernanceVerdict,
        reason: str, confidence: float, payload: dict,
    ) -> None:
        rec = AuthorizationRecord(
            source=source, recommendation_type=rec_type, verdict=verdict.value,
            reason=reason, confidence=confidence,
            payload={k: payload[k] for k in list(payload)[:8]},  # bound payload size
        )
        with self._lock:
            self._auth_log.append(rec)
            self._counts[verdict.value] = self._counts.get(verdict.value, 0) + 1

    def _record_promotion(
        self, module: str, from_stage: PromotionStage, to_stage: PromotionStage,
        verdict: GovernanceVerdict, reason: str, metrics: dict,
    ) -> None:
        rec = PromotionRecord(
            module=module, from_stage=from_stage.value, to_stage=to_stage.value,
            verdict=verdict.value, reason=reason,
            metrics={k: metrics[k] for k in list(metrics)[:8]},
        )
        with self._lock:
            self._promotion_log.append(rec)

    def recent_authorizations(self, limit: Optional[int] = None) -> List[dict]:
        with self._lock:
            items = [r.to_dict() for r in self._auth_log]
        if limit is not None and limit >= 0:
            return items[-limit:]
        return items

    def recent_promotions(self, limit: Optional[int] = None) -> List[dict]:
        with self._lock:
            items = [r.to_dict() for r in self._promotion_log]
        if limit is not None and limit >= 0:
            return items[-limit:]
        return items

    def toxic_pairs(self) -> List[dict]:
        with self._lock:
            return [r.to_dict() for r in self._toxic_pairs.values()]

    def get_status(self) -> dict:
        """Summary for the dashboard / ops."""
        with self._lock:
            counts = dict(self._counts)
            auth_n = len(self._auth_log)
            promo_n = len(self._promotion_log)
            toxic = [r.to_dict() for r in self._toxic_pairs.values()]
        return {
            "enforce_toxic_pairs": self._enforce_toxic,
            "has_module_governor": self._module_governor is not None,
            "has_tuner_agent": self._tuner_agent is not None,
            "has_virtual_registry": self._virtual_registry is not None,
            "decision_counts": counts,
            "authorizations_logged": auth_n,
            "promotions_logged": promo_n,
            "toxic_pairs": toxic,
            "bounds": {
                "size_multiplier": [self._min_size_mult, self._max_size_mult],
                "max_weight_multiplier": self._max_weight_mult,
            },
        }


# ── Helpers ──────────────────────────────────────────────────────────────


def _as_dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _as_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = ["GovernanceDivision"]
