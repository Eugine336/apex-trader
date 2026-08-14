"""APEX TRADER — Model Manager (Constitution Part XVI, Article 9).

The AI Cognitive Brain shall never depend on one language model. OpenAI, Llama,
DeepSeek, Anthropic, Qwen, Mistral and every future model are *implementations*;
the Brain reasons, and the **Model Manager** selects the reasoning model by
policy — capability, latency, cost, availability and performance.

This manager is a drop-in replacement for a single
:class:`~llm.client.LLMClient` wherever a reasoner expects a client: it exposes
the same duck-typed surface (``usable``, ``complete(system, user)``, ``model``,
``describe()``) but holds *many* candidate clients and, on each call, selects the
best usable one by the configured policy and fails over to the next on error.
Per-candidate health (successes, failures, EWMA latency) is tracked so the
"performance" policy can prefer models that are actually working.

Adding or removing a model is a configuration change (a new candidate here) and
never touches the Brain (Part XVI Art 8/14). Pure standard library; fail-safe —
a manager fault yields ``None`` exactly like a single client would.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from llm.health import CircuitBreaker, CircuitConfig
from llm.provider_tiers import ProviderTier, resolve_tier, tier_label

# Selection policies.
POLICY_PRIORITY = "priority"          # lowest priority number first (operator order)
POLICY_PERFORMANCE = "performance"    # best observed success-rate / latency first
_POLICIES = frozenset({POLICY_PRIORITY, POLICY_PERFORMANCE})


@dataclass
class _Candidate:
    """One reasoning model plus its selection metadata and live health."""

    client: Any
    priority: int = 100               # lower = preferred (operator intent)
    cost: float = 0.0                 # relative cost hint (lower preferred on ties)
    # Part XXIII Art 5/6/7/14 — provider tier (1 frontier, 2 hosted, 3 local,
    # 4 unknown). Leads failover order so Tier 1 is tried before Tier 2 before
    # Tier 3; within a tier the configured policy governs.
    tier: int = int(ProviderTier.UNKNOWN)
    # Compute-class routing (GPU/Compute Constitution §8/§11): the reasoning
    # classes this model may serve (e.g. {"fast","deep"}). Empty ⇒ serves ANY
    # class. ``context_window`` is the model's context in tokens (0 ⇒ unknown ⇒
    # assumed sufficient) so a request needing a large context can skip a model
    # that cannot hold it.
    classes: frozenset = field(default_factory=frozenset)
    context_window: int = 0
    # Live health + circuit breaker (§7/§9). Owns successes/failures/latency and
    # the OPEN/HALF_OPEN/CLOSED state so a failing provider is not re-hammered.
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)

    @property
    def usable(self) -> bool:
        try:
            return bool(getattr(self.client, "usable", False))
        except Exception:  # noqa: BLE001
            return False

    @property
    def model(self) -> str:
        return str(getattr(self.client, "model", "") or "")

    # Stats delegate to the circuit breaker (single source of truth).
    @property
    def successes(self) -> int:
        return self.breaker.successes

    @property
    def failures(self) -> int:
        return self.breaker.failures

    @property
    def ewma_latency_ms(self) -> float:
        return self.breaker.ewma_latency_ms

    @property
    def attempts(self) -> int:
        return self.breaker.attempts

    @property
    def success_rate(self) -> float:
        return self.breaker.success_rate

    def available(self, now: Optional[float] = None) -> bool:
        """Usable config AND the circuit admits (healthy / half-open probe)."""
        return self.usable and self.breaker.admits(now)

    def serves(self, compute_class: Optional[str], min_context: Optional[int]) -> bool:
        """True if this model may serve the requested class + context window."""
        if compute_class:
            if self.classes and str(compute_class).strip().lower() not in self.classes:
                return False
        if min_context and int(min_context) > 0:
            if self.context_window and self.context_window < int(min_context):
                return False
        return True

    def record(self, ok: bool, latency_ms: float, error: str = "") -> None:
        if ok:
            self.breaker.record_success(latency_ms)
        else:
            self.breaker.record_failure(latency_ms, error)

    def to_dict(self) -> dict:
        d = {
            "model": self.model,
            "provider": str(getattr(self.client, "provider", "") or ""),
            "priority": self.priority,
            "tier": int(self.tier),
            "tier_label": tier_label(ProviderTier(self.tier))
            if self.tier in (1, 2, 3, 4) else "unknown",
            "cost": round(self.cost, 4),
            "classes": sorted(self.classes),
            "context_window": self.context_window,
            "usable": self.usable,
            "available": self.available(),
        }
        d.update(self.breaker.to_dict())
        return d


class ModelManager:
    """Selects among several reasoning models by policy; fails over on error.

    Drop-in for a single client: exposes ``usable`` / ``complete`` / ``model`` /
    ``describe``. Never raises — a call returns ``None`` if every usable
    candidate fails, exactly like a lone client.
    """

    def __init__(self, candidates: list, *, policy: str = POLICY_PRIORITY) -> None:
        self._candidates: list = [c for c in (candidates or []) if isinstance(c, _Candidate)]
        self.policy = policy if policy in _POLICIES else POLICY_PRIORITY
        self._lock = threading.Lock()
        self._selections = 0
        self._failovers = 0
        self._last_selected: Optional[_Candidate] = None

    # ── Duck-typed client surface ──────────────────────────────────────────

    @property
    def usable(self) -> bool:
        return any(c.usable for c in self._candidates)

    @property
    def model(self) -> str:
        if self._last_selected is not None:
            return self._last_selected.model
        for c in self._candidates:
            if c.usable:
                return c.model
        return self._candidates[0].model if self._candidates else ""

    @property
    def provider(self) -> str:
        c = self._last_selected
        return str(getattr(c.client, "provider", "") or "") if c is not None else ""

    def _ordered(self, now: Optional[float] = None,
                 compute_class: Optional[str] = None,
                 min_context: Optional[int] = None) -> list:
        """Eligible candidates in failover order (best first). Deterministic.

        Eligible = usable config AND the circuit admits (healthy or half-open) —
        a provider tripped OPEN is skipped until its cooldown elapses, so a dead
        provider is not re-hammered every cycle (§7/§9). Then a compute-class /
        context-window filter (§8) removes models that cannot serve the request;
        if that would leave nobody, it falls back to all healthy candidates (a
        generalist answer beats none). Part XXIII Art 14 — tier leads the order;
        WITHIN a tier the configured policy governs."""
        healthy = [c for c in self._candidates if c.available(now)]
        eligible = [c for c in healthy if c.serves(compute_class, min_context)]
        pool = eligible or healthy
        if self.policy == POLICY_PERFORMANCE:
            # Tier, then best success-rate, then lowest latency, priority, cost.
            pool.sort(key=lambda c: (c.tier, -c.success_rate, c.ewma_latency_ms,
                                     c.priority, c.cost))
        else:  # POLICY_PRIORITY
            # Tier, then operator priority, then fewer failures, then cost.
            pool.sort(key=lambda c: (c.tier, c.priority, c.failures, c.cost))
        return pool

    def complete(self, system: str, user: str, *,
                 compute_class: Optional[str] = None,
                 min_context: Optional[int] = None) -> Optional[str]:
        """Select a model by policy and complete; fail over on error. Fail-safe.

        ``compute_class`` / ``min_context`` let the Brain request a computational
        CLASS (e.g. "deep") and a minimum context window; the router serves it
        from an eligible, HEALTHY provider (never a tripped-open one) and returns
        to a superior provider automatically the moment it recovers (each call
        re-ranks from the top)."""
        now = time.monotonic()
        ordered = self._ordered(now, compute_class, min_context)
        if not ordered:
            return None
        for idx, cand in enumerate(ordered):
            t0 = time.time()
            err = ""
            try:
                reply = cand.client.complete(system, user)
            except Exception as exc:  # noqa: BLE001 — a model fault is a failover, not a crash
                logger.debug("[model-manager] {} raised: {}", cand.model, exc)
                reply = None
                err = f"{type(exc).__name__}: {exc}"
            latency_ms = (time.time() - t0) * 1000.0
            ok = bool(reply)
            with self._lock:
                cand.record(ok, latency_ms, err)
                if ok:
                    self._selections += 1
                    self._last_selected = cand
                    if idx > 0:
                        self._failovers += 1
            if ok:
                if idx > 0:
                    # Part XXIII/XXIV — reasoning failed over: name the model that
                    # answered and how many ahead of it were down, so the operator
                    # can see who is still standing in the failsafe chain.
                    logger.info(
                        "[model-manager] reasoning answered by {} (Tier {}) after {} unavailable ahead of it",
                        cand.model, cand.tier, idx,
                    )
                return reply
            # The client logs each provider's down transition once; keep the
            # per-attempt failover step at DEBUG so a persistently unavailable
            # candidate does not flood the log every cycle.
            logger.debug("[model-manager] {} unavailable — trying next in the chain", cand.model)
        return None

    def describe(self) -> dict:
        with self._lock:
            selected = self._last_selected.model if self._last_selected else None
            return {
                "manager": True,
                "policy": self.policy,
                "usable": self.usable,
                "candidate_count": len(self._candidates),
                "usable_count": sum(1 for c in self._candidates if c.usable),
                "healthy_count": sum(1 for c in self._candidates if c.available()),
                "selected_model": selected,
                "selections": self._selections,
                "failovers": self._failovers,
                "candidates": [c.to_dict() for c in self._candidates],
            }


def _coerce_specs(config: Any) -> list:
    """Return the list of extra-model spec dicts from config (fail-safe)."""
    raw = getattr(config, "extra_models", None)
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict)]
    return []


def _circuit_config(config: Any) -> CircuitConfig:
    """Build the shared circuit-breaker tuning from an LLMConfig-like object."""
    return CircuitConfig(
        failure_threshold=int(getattr(config, "circuit_failure_threshold", 3) or 3),
        cooldown_seconds=float(getattr(config, "circuit_cooldown_seconds", 30.0) or 30.0),
        cooldown_max_seconds=float(
            getattr(config, "circuit_cooldown_max_seconds", 300.0) or 300.0
        ),
    )


def spec_classes(spec: dict) -> frozenset:
    """Compute classes a model spec declares (empty ⇒ serves any class)."""
    raw = spec.get("classes", spec.get("compute_classes", []))
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(c).strip().lower() for c in raw if str(c).strip())


def spec_context_window(spec: dict) -> int:
    try:
        return max(0, int(spec.get("context_window", 0) or 0))
    except (TypeError, ValueError):
        return 0


def build_model_manager(
    config: Any,
    transport: Optional[Any] = None,
    *,
    budget_ledger: Optional[Any] = None,
) -> Optional[Any]:
    """Build a :class:`ModelManager` from an ``LLMConfig``-like object.

    The primary model (``provider``/``model``/``api_key``/``base_url``) is the
    first candidate; each entry in ``config.extra_models`` becomes an additional
    candidate, inheriting the primary's key/base_url when it omits its own (the
    common "one gateway, many models" case). A spec may declare ``classes`` (the
    compute classes it serves) and ``context_window`` for compute-class routing.
    Returns ``None`` when no usable candidate can be built — a safe no-op,
    exactly like ``build_client``. When quota limits are configured, each
    candidate gets a shared per-account budget meter (§22–§28); ``budget_ledger``
    overrides the process-shared ledger (tests / a scoped subsystem).
    """
    from llm.client import LLMClient, attach_budget  # local import keeps this module lightweight
    from llm.provider_budget import coerce_limits, get_shared_ledger
    from llm.provider_credentials import (
        has_credentials, resolve_api_key, resolve_base_url,
    )

    ledger = budget_ledger if budget_ledger is not None else get_shared_ledger()
    cc = _circuit_config(config)
    candidates: list = []
    primary_key = str(getattr(config, "api_key", "") or "")
    primary_base = str(getattr(config, "base_url", "") or "")
    primary_provider = str(getattr(config, "provider", "") or "")

    def _entry_limits(spec: dict, prov: str) -> dict:
        # A roster entry's own limits; a same-provider entry with none inherits
        # the primary's (free-tier limits are per account, mirroring key
        # inheritance). Cross-provider entries never inherit.
        rpm, rpd, tpm, tpd = coerce_limits(spec)
        if not (rpm or rpd or tpm or tpd) and \
                str(prov).strip().lower() == primary_provider.strip().lower():
            rpm, rpd, tpm, tpd = coerce_limits(config)
        return {"rpm": rpm, "rpd": rpd, "tpm": tpm, "tpd": tpd}

    def _mk(provider, model, api_key, base_url, timeout, max_tokens, temperature):
        if not provider or not model:
            return None
        return LLMClient(
            provider=str(provider), model=str(model),
            api_key=str(api_key or ""), base_url=str(base_url or ""),
            timeout_seconds=float(timeout), max_tokens=int(max_tokens),
            temperature=float(temperature), transport=transport,
        )

    to = float(getattr(config, "timeout_seconds", 20.0) or 20.0)
    mt = int(getattr(config, "max_tokens", 512) or 512)
    tmp = float(getattr(config, "temperature", 0.2) or 0.2)

    primary = _mk(getattr(config, "provider", ""), getattr(config, "model", ""),
                  primary_key, primary_base, to, mt, tmp)
    if primary is not None and has_credentials(primary_provider, primary_key):
        attach_budget(primary, config, provider=primary_provider, budget_ledger=ledger)
        candidates.append(_Candidate(
            client=primary, priority=0,
            tier=int(resolve_tier(primary_provider)),
            breaker=CircuitBreaker(cc),
        ))

    for i, spec in enumerate(_coerce_specs(config), start=1):
        prov = spec.get("provider", primary_provider)
        # Per-provider credential resolution (Part XXIII Art 15): a blank entry
        # resolves its own <PROVIDER>_API_KEY; the primary key/base are inherited
        # only for the SAME provider (never cross-vendor). A credential-requiring
        # provider with no key is skipped — inert, never selected or called.
        key = resolve_api_key(prov, spec.get("api_key", ""),
                              primary_provider=primary_provider, primary_key=primary_key)
        base = resolve_base_url(prov, spec.get("base_url", ""),
                               primary_provider=primary_provider, primary_base=primary_base)
        if not has_credentials(prov, key):
            continue
        client = _mk(
            prov, spec.get("model", ""), key, base,
            spec.get("timeout_seconds", to), spec.get("max_tokens", mt),
            spec.get("temperature", tmp),
        )
        if client is not None:
            attach_budget(client, _entry_limits(spec, prov), provider=prov, budget_ledger=ledger)
            candidates.append(_Candidate(
                client=client,
                priority=int(spec.get("priority", i)),
                cost=float(spec.get("cost", 0.0) or 0.0),
                tier=int(resolve_tier(prov, spec.get("tier"))),
                classes=spec_classes(spec),
                context_window=spec_context_window(spec),
                breaker=CircuitBreaker(cc),
            ))

    usable = [c for c in candidates if c.usable]
    if not usable:
        return None
    policy = str(getattr(config, "model_policy", POLICY_PRIORITY) or POLICY_PRIORITY)
    return ModelManager(candidates, policy=policy)


__all__ = ["ModelManager", "build_model_manager", "POLICY_PRIORITY", "POLICY_PERFORMANCE"]
