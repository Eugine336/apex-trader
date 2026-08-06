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
from governance.health_models import HealthAssessment, HealthStatus
from governance.verdict import GovernanceVerdict

# Recommendation type constants (kept as literals so this module does not import
# the Learning layer — it only authorises objects that carry these strings).
_WEIGHT_UPDATE = "WEIGHT_UPDATE"
_AVOID_PATTERN = "AVOID_PATTERN"
_SIZE_ADJUST = "SIZE_ADJUST"
_PROFILE_CHANGE = "PROFILE_CHANGE"
_MODULE_SUPPRESS = "MODULE_SUPPRESS"
_TOXIC_PAIR_BLOCK = "TOXIC_PAIR_BLOCK"
_PARAM_PROMOTE = "PARAM_PROMOTE"

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
        _PARAM_PROMOTE,
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
        # Aggregate-health monitoring (the thermostat over all learning). When a
        # HealthAssessor is wired, Governance can freeze the whole learning
        # layer on CRITICAL aggregate health and release it on recovery.
        health_assessor: Optional[object] = None,
        health_auto_freeze: bool = True,
        health_auto_release: bool = True,
        # Per-gate parameter attribution (which learned gate adjustment opened a
        # trade). Observational — surfaced in get_status and used to warn when
        # learner-decisive trades are losing. Never freezes / blocks on its own.
        gate_attributor: Optional[object] = None,
        # Persistent competing Long/Short/Flat theses (Gap 1a). Observational —
        # surfaced in get_status. Never freezes / blocks on its own.
        thesis_engine: Optional[object] = None,
        # Evolving market campaigns — the lifetime of a directional thesis per
        # symbol. Observational — surfaced in get_status. Never blocks.
        campaign_registry: Optional[object] = None,
        # LLM reasoning subsystem — emits opinions as evidence. Observational —
        # surfaced in get_status (secret-safe). Never blocks / overrides.
        llm_reasoner: Optional[object] = None,
        # Autonomous Action Layer gateway — surfaced in get_status (secret-safe).
        # Governs/executes Brain-authored objectives; never reasons.
        action_orchestrator: Optional[object] = None,
        # AI Cognitive Brain (Single Reasoner) + its loop — surfaced in
        # get_status. The Brain reasons; Governance only observes it here.
        cognitive_brain: Optional[object] = None,
        cognition_loop: Optional[object] = None,
        cognition_gate: Optional[object] = None,
        management_gate: Optional[object] = None,
        campaign_memory: Optional[object] = None,
        action_planner: Optional[object] = None,
        operations_author: Optional[object] = None,
        influence_ledger: Optional[object] = None,
        brain_calibration: Optional[object] = None,
        reasoning_orchestrator: Optional[object] = None,
        provider_registry: Optional[object] = None,
        cognition_observability: Optional[object] = None,
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

        # Aggregate-health monitoring + the auto-freeze/release state machine.
        self._health_assessor = health_assessor
        self._health_auto_freeze = bool(health_auto_freeze)
        self._health_auto_release = bool(health_auto_release)
        # True while learning is frozen *because of* aggregate health (kept
        # distinct so we freeze/release exactly once per transition rather than
        # spamming the TunerAgent on every close).
        self._health_frozen = False
        self._last_health: Optional[HealthAssessment] = None

        # Per-gate attribution (observational). Read in get_status; used in
        # check_health_after_close to surface a warning when the trades a
        # learned gate loosening *opened* are net-losing.
        self._gate_attributor = gate_attributor

        # Persistent competing theses (observational). Read in get_status only.
        self._thesis_engine = thesis_engine

        # Evolving market campaigns (observational). Read in get_status only.
        self._campaign_registry = campaign_registry

        # LLM reasoning subsystem (observational). Read in get_status only.
        self._llm_reasoner = llm_reasoner

        # Autonomous Action Layer gateway (observational). Read in get_status.
        self._action_orchestrator = action_orchestrator

        # AI Cognitive Brain + loop (observational). Read in get_status only.
        self._cognitive_brain = cognitive_brain
        self._cognition_loop = cognition_loop
        self._cognition_gate = cognition_gate
        self._management_gate = management_gate
        # Institutional memory (Phase H, Part VII). Observational — read in
        # get_status only; the Brain/loop own the actual reads and writes.
        self._campaign_memory = campaign_memory
        # Action Planner + operations author (Phase I, Part IX). Observational —
        # read in get_status only; they own capability/provider selection and
        # operational objective authoring, never market reasoning.
        self._action_planner = action_planner
        self._operations_author = operations_author
        # Adaptive influence + Brain calibration (Phase J, Part VIII).
        # Observational — read in get_status only.
        self._influence_ledger = influence_ledger
        self._brain_calibration = brain_calibration
        # Reasoning Orchestrator (Part XVII) — multi-engine consultative reasoning.
        # Observational — read in get_status only; advisory, never authority.
        self._reasoning_orchestrator = reasoning_orchestrator
        # Provider Registry / Manager (Part XXI) — catalogue of every provider
        # and its constitutional state. Observational — read in get_status only.
        self._provider_registry = provider_registry
        # Cognition observability (Phase L, Part XII) — read-only derived metrics.
        self._cognition_observability = cognition_observability

    # ── Wiring (injected after construction) ──────────────────────────────

    def bind_runtime(
        self,
        *,
        module_governor: Optional[object] = None,
        tuner_agent: Optional[object] = None,
        virtual_registry: Optional[object] = None,
        health_assessor: Optional[object] = None,
        gate_attributor: Optional[object] = None,
        thesis_engine: Optional[object] = None,
        campaign_registry: Optional[object] = None,
        llm_reasoner: Optional[object] = None,
        action_orchestrator: Optional[object] = None,
        cognitive_brain: Optional[object] = None,
        cognition_loop: Optional[object] = None,
        cognition_gate: Optional[object] = None,
        management_gate: Optional[object] = None,
        campaign_memory: Optional[object] = None,
        action_planner: Optional[object] = None,
        operations_author: Optional[object] = None,
        influence_ledger: Optional[object] = None,
        brain_calibration: Optional[object] = None,
        reasoning_orchestrator: Optional[object] = None,
        provider_registry: Optional[object] = None,
        cognition_observability: Optional[object] = None,
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
        if health_assessor is not None:
            self._health_assessor = health_assessor
        if gate_attributor is not None:
            self._gate_attributor = gate_attributor
        if thesis_engine is not None:
            self._thesis_engine = thesis_engine
        if campaign_registry is not None:
            self._campaign_registry = campaign_registry
        if llm_reasoner is not None:
            self._llm_reasoner = llm_reasoner
        if action_orchestrator is not None:
            self._action_orchestrator = action_orchestrator
        if cognitive_brain is not None:
            self._cognitive_brain = cognitive_brain
        if cognition_loop is not None:
            self._cognition_loop = cognition_loop
        if cognition_gate is not None:
            self._cognition_gate = cognition_gate
        if management_gate is not None:
            self._management_gate = management_gate
        if campaign_memory is not None:
            self._campaign_memory = campaign_memory
        if action_planner is not None:
            self._action_planner = action_planner
        if operations_author is not None:
            self._operations_author = operations_author
        if influence_ledger is not None:
            self._influence_ledger = influence_ledger
        if brain_calibration is not None:
            self._brain_calibration = brain_calibration
        if reasoning_orchestrator is not None:
            self._reasoning_orchestrator = reasoning_orchestrator
        if provider_registry is not None:
            self._provider_registry = provider_registry
        if cognition_observability is not None:
            self._cognition_observability = cognition_observability

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
        if rec_type == _PARAM_PROMOTE:
            return self._eval_param_promote(payload)
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

    def _eval_param_promote(self, payload: dict) -> Tuple[GovernanceVerdict, str]:
        """A ParameterEvolver promotion is authorised only when it names a
        parameter and carries a finite proposed value.

        The evolver has already walk-forward + shadow-validated the candidate;
        Governance's job here is the boundary check (named parameter, finite
        value) and the audit record — it never applies the value itself
        (the evolver runs in recommend-only / shadow mode)."""
        param_name = str(payload.get("param_name") or "").strip()
        if not param_name:
            return GovernanceVerdict.REJECTED, "param promote with no parameter name"
        proposed = _as_float(payload.get("proposed_value"), default=None)
        if proposed is None or not math.isfinite(proposed):
            return (
                GovernanceVerdict.REJECTED,
                f"non-finite proposed value for {param_name}",
            )
        return (
            GovernanceVerdict.AUTHORIZED,
            f"param {param_name} -> {proposed:g} authorised",
        )

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

    # ── Aggregate-health thermostat (freeze/resume the whole learning layer) ─

    def check_health(self) -> Optional[HealthAssessment]:
        """Read the aggregate-health verdict and act on it.

        On ``CRITICAL`` (negative rolling expectancy *and* accelerating
        participation or heavy learner-enabled losses) Governance freezes every
        registered tunable — pausing learning so it stops adjusting parameters
        while the system is bleeding. On a return to ``HEALTHY`` the freeze is
        released and learning resumes. ``DEGRADED`` is a visible warning that,
        on its own, changes nothing.

        The freeze/release is edge-triggered: a one-shot ``_health_frozen`` flag
        means we order the TunerAgent exactly once per transition, never once
        per trade close. Fail-safe — any fault returns ``None`` and never
        freezes (a monitoring error must not pause trading).

        Returns the :class:`HealthAssessment` (or ``None`` when no assessor is
        wired or the assessment could not be computed).
        """
        assessor = self._health_assessor
        if assessor is None:
            return None
        try:
            assessment = assessor.assess()
        except Exception as exc:  # noqa: BLE001 — fail-safe: never freeze on a fault
            logger.debug("[governance] health assessment failed: {}", exc)
            return None

        action: Optional[str] = None
        with self._lock:
            self._last_health = assessment
            status = assessment.health_status
            if (
                status == HealthStatus.CRITICAL
                and self._health_auto_freeze
                and not self._health_frozen
            ):
                self._health_frozen = True
                action = "freeze"
            elif (
                status == HealthStatus.HEALTHY
                and self._health_auto_release
                and self._health_frozen
            ):
                self._health_frozen = False
                action = "release"

        # Perform the containment outside the lock (freeze/release of the
        # TunerAgent take their own locks); the flag above already guarantees
        # this fires exactly once per transition.
        if action == "freeze":
            frozen = self._freeze_all_tuning(
                reason=(
                    f"aggregate health CRITICAL "
                    f"(EV={assessment.rolling_ev:+.3f}, "
                    f"entry_rate={assessment.entry_rate_trend:.2f}, "
                    f"learner_loss={assessment.learner_enabled_loss_rate:.2f})"
                )
            )
            logger.warning(
                "[governance] system health CRITICAL — froze {} tunable(s); "
                "learning PAUSED until recovery", frozen,
            )
        elif action == "release":
            released = self._release_all_tuning()
            logger.info(
                "[governance] system health recovered to HEALTHY — released {} "
                "tunable(s); learning RESUMED", released,
            )
        return assessment

    def check_health_after_close(
        self,
        realized_r: float,
        entry_path: str = "",
        learner_enabled: bool = False,
    ) -> Optional[HealthAssessment]:
        """Record one closed trade with the assessor, then re-check health.

        Called from the trade-close path. Fail-safe — never raises, so a
        monitoring fault can never break the close path."""
        assessor = self._health_assessor
        if assessor is None:
            return None
        try:
            assessor.record_trade_close(
                realized_r=realized_r,
                entry_path=entry_path,
                learner_enabled=learner_enabled,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[governance] health record_trade_close failed: {}", exc)
        self._warn_on_decisive_losses()
        return self.check_health()

    def _warn_on_decisive_losses(self) -> None:
        """Surface a warning when learner-decisive trades are net-losing.

        Observational only — gate attribution never freezes or blocks. A high
        ``decisive_negative_ev_count`` means a learned gate loosening is opening
        trades the operator's default thresholds would have rejected, and those
        trades are losing. Fail-safe — never raises to the close path.
        """
        attributor = self._gate_attributor
        if attributor is None:
            return
        try:
            summary = attributor.summarize()
            neg_decisive = int(summary.get("decisive_negative_ev_count", 0) or 0)
            if neg_decisive >= 3:
                logger.warning(
                    "⚠️ Gate attribution: {} learner-decisive trade(s) closed at "
                    "negative R — a learned gate loosening is opening trades the "
                    "default thresholds would have rejected, and they are losing "
                    "(decisive avg R {:+.3f})",
                    neg_decisive,
                    float(summary.get("decisive_avg_r", 0.0) or 0.0),
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[governance] gate-attribution warn failed: {}", exc)

    def _freeze_all_tuning(self, *, reason: str = "") -> int:
        """Freeze every registered tunable via the TunerAgent. Returns count."""
        agent = self._tuner_agent
        names = getattr(agent, "registered_names", None) if agent is not None else None
        if not names:
            return 0
        frozen = 0
        for name in list(names):
            if self.freeze_tuning(name, reason=reason):
                frozen += 1
        return frozen

    def _release_all_tuning(self) -> int:
        """Release every registered tunable via the TunerAgent. Returns count."""
        agent = self._tuner_agent
        names = getattr(agent, "registered_names", None) if agent is not None else None
        if not names:
            return 0
        released = 0
        for name in list(names):
            if self.release_tuning(name):
                released += 1
        return released

    @property
    def learning_frozen_for_health(self) -> bool:
        """True while learning is paused by the aggregate-health thermostat."""
        with self._lock:
            return self._health_frozen

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
            health_frozen = self._health_frozen
            last_health = self._last_health
        health: dict = {
            "has_assessor": self._health_assessor is not None,
            "auto_freeze_on_critical": self._health_auto_freeze,
            "auto_release_on_healthy": self._health_auto_release,
            "learning_frozen_for_health": health_frozen,
            "last_assessment": last_health.to_dict() if last_health is not None else None,
        }
        gate_attribution = None
        if self._gate_attributor is not None:
            try:
                gate_attribution = self._gate_attributor.summarize()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[governance] gate-attribution status failed: {}", exc)
        thesis_engine = None
        if self._thesis_engine is not None:
            try:
                thesis_engine = self._thesis_engine.get_status()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[governance] thesis-engine status failed: {}", exc)
        campaign_registry = None
        if self._campaign_registry is not None:
            try:
                campaign_registry = self._campaign_registry.get_status()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[governance] campaign-registry status failed: {}", exc)
        llm_reasoner = None
        if self._llm_reasoner is not None:
            try:
                llm_reasoner = self._llm_reasoner.get_status()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[governance] llm-reasoner status failed: {}", exc)
        action_layer = None
        if self._action_orchestrator is not None:
            try:
                action_layer = self._action_orchestrator.get_status()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[governance] action-layer status failed: {}", exc)
        cognition = None
        if (self._cognitive_brain is not None or self._cognition_loop is not None
                or self._cognition_gate is not None or self._management_gate is not None
                or self._campaign_memory is not None or self._action_planner is not None
                or self._operations_author is not None or self._influence_ledger is not None
                or self._brain_calibration is not None
                or self._reasoning_orchestrator is not None
                or self._cognition_observability is not None):
            cognition = {}
            if self._cognitive_brain is not None:
                try:
                    cognition["brain"] = self._cognitive_brain.get_status()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[governance] cognitive-brain status failed: {}", exc)
            if self._cognition_loop is not None:
                try:
                    cognition["loop"] = self._cognition_loop.get_status()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[governance] cognition-loop status failed: {}", exc)
            if self._cognition_gate is not None:
                try:
                    cognition["gate"] = self._cognition_gate.get_status()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[governance] cognition-gate status failed: {}", exc)
            if self._management_gate is not None:
                try:
                    cognition["management_gate"] = self._management_gate.get_status()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[governance] management-gate status failed: {}", exc)
            if self._campaign_memory is not None:
                try:
                    cognition["memory"] = self._campaign_memory.get_status()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[governance] campaign-memory status failed: {}", exc)
            if self._action_planner is not None:
                try:
                    cognition["planner"] = self._action_planner.get_status()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[governance] action-planner status failed: {}", exc)
            if self._operations_author is not None:
                try:
                    cognition["operations"] = self._operations_author.get_status()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[governance] operations-author status failed: {}", exc)
            if self._influence_ledger is not None:
                try:
                    cognition["influence"] = self._influence_ledger.get_status()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[governance] influence-ledger status failed: {}", exc)
            if self._brain_calibration is not None:
                try:
                    cognition["calibration"] = self._brain_calibration.get_status()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[governance] brain-calibration status failed: {}", exc)
            if self._reasoning_orchestrator is not None:
                try:
                    cognition["reasoning_orchestrator"] = self._reasoning_orchestrator.get_status()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[governance] reasoning-orchestrator status failed: {}", exc)
            if self._provider_registry is not None:
                try:
                    cognition["provider_registry"] = self._provider_registry.get_status()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[governance] provider-registry status failed: {}", exc)
            if self._cognition_observability is not None:
                try:
                    cognition["observability"] = self._cognition_observability.get_status()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[governance] cognition-observability status failed: {}", exc)
        return {
            "enforce_toxic_pairs": self._enforce_toxic,
            "has_module_governor": self._module_governor is not None,
            "has_tuner_agent": self._tuner_agent is not None,
            "has_virtual_registry": self._virtual_registry is not None,
            "decision_counts": counts,
            "authorizations_logged": auth_n,
            "promotions_logged": promo_n,
            "toxic_pairs": toxic,
            "health": health,
            "gate_attribution": gate_attribution,
            "thesis_engine": thesis_engine,
            "campaign_registry": campaign_registry,
            "llm_reasoner": llm_reasoner,
            "action_layer": action_layer,
            "cognition": cognition,
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
