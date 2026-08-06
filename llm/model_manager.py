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
    successes: int = 0
    failures: int = 0
    ewma_latency_ms: float = 0.0

    @property
    def usable(self) -> bool:
        try:
            return bool(getattr(self.client, "usable", False))
        except Exception:  # noqa: BLE001
            return False

    @property
    def model(self) -> str:
        return str(getattr(self.client, "model", "") or "")

    @property
    def attempts(self) -> int:
        return self.successes + self.failures

    @property
    def success_rate(self) -> float:
        # Optimistic prior so an untried model is tried before a known-bad one.
        return 1.0 if self.attempts == 0 else self.successes / self.attempts

    def record(self, ok: bool, latency_ms: float) -> None:
        if ok:
            self.successes += 1
        else:
            self.failures += 1
        # EWMA so recent latency dominates without unbounded history.
        a = 0.3
        self.ewma_latency_ms = (latency_ms if self.ewma_latency_ms <= 0.0
                                else (1 - a) * self.ewma_latency_ms + a * latency_ms)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "provider": str(getattr(self.client, "provider", "") or ""),
            "priority": self.priority,
            "tier": int(self.tier),
            "tier_label": tier_label(ProviderTier(self.tier))
            if self.tier in (1, 2, 3, 4) else "unknown",
            "cost": round(self.cost, 4),
            "usable": self.usable,
            "successes": self.successes,
            "failures": self.failures,
            "success_rate": round(self.success_rate, 4),
            "ewma_latency_ms": round(self.ewma_latency_ms, 1),
        }


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

    def _ordered(self) -> list:
        """Usable candidates in failover order (best first). Deterministic.

        Part XXIII Art 14 — tier leads the order (Tier 1 before Tier 2 before
        Tier 3 before unknown), so a Tier-1 failure fails over to Tier 2, then
        Tier 3, then local. WITHIN a tier the configured policy governs. When
        every candidate shares a tier (the common single-tier / single-provider
        case) the tier key is constant and ordering reduces exactly to the prior
        policy behaviour."""
        usable = [c for c in self._candidates if c.usable]
        if self.policy == POLICY_PERFORMANCE:
            # Tier, then best success-rate, then lowest latency, priority, cost.
            usable.sort(key=lambda c: (c.tier, -c.success_rate, c.ewma_latency_ms,
                                       c.priority, c.cost))
        else:  # POLICY_PRIORITY
            # Tier, then operator priority, then fewer failures, then cost.
            usable.sort(key=lambda c: (c.tier, c.priority, c.failures, c.cost))
        return usable

    def complete(self, system: str, user: str) -> Optional[str]:
        """Select a model by policy and complete; fail over on error. Fail-safe."""
        ordered = self._ordered()
        if not ordered:
            return None
        for idx, cand in enumerate(ordered):
            t0 = time.time()
            try:
                reply = cand.client.complete(system, user)
            except Exception as exc:  # noqa: BLE001 — a model fault is a failover, not a crash
                logger.debug("[model-manager] {} raised: {}", cand.model, exc)
                reply = None
            latency_ms = (time.time() - t0) * 1000.0
            ok = bool(reply)
            with self._lock:
                cand.record(ok, latency_ms)
                if ok:
                    self._selections += 1
                    self._last_selected = cand
                    if idx > 0:
                        self._failovers += 1
                else:
                    self._failovers += 1 if idx < len(ordered) - 1 else 0
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
            else:
                logger.info("[model-manager] {} unavailable — trying next in the chain", cand.model)
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


def build_model_manager(config: Any, transport: Optional[Any] = None) -> Optional[Any]:
    """Build a :class:`ModelManager` from an ``LLMConfig``-like object.

    The primary model (``provider``/``model``/``api_key``/``base_url``) is the
    first candidate; each entry in ``config.extra_models`` becomes an additional
    candidate, inheriting the primary's key/base_url when it omits its own (the
    common "one gateway, many models" case). Returns ``None`` when no usable
    candidate can be built — a safe no-op, exactly like ``build_client``.
    """
    from llm.client import LLMClient  # local import keeps this module lightweight
    from llm.provider_credentials import (
        has_credentials, resolve_api_key, resolve_base_url,
    )

    candidates: list = []
    primary_key = str(getattr(config, "api_key", "") or "")
    primary_base = str(getattr(config, "base_url", "") or "")
    primary_provider = str(getattr(config, "provider", "") or "")

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
        candidates.append(_Candidate(
            client=primary, priority=0,
            tier=int(resolve_tier(primary_provider)),
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
            candidates.append(_Candidate(
                client=client,
                priority=int(spec.get("priority", i)),
                cost=float(spec.get("cost", 0.0) or 0.0),
                tier=int(resolve_tier(prov, spec.get("tier"))),
            ))

    usable = [c for c in candidates if c.usable]
    if not usable:
        return None
    policy = str(getattr(config, "model_policy", POLICY_PRIORITY) or POLICY_PRIORITY)
    return ModelManager(candidates, policy=policy)


__all__ = ["ModelManager", "build_model_manager", "POLICY_PRIORITY", "POLICY_PERFORMANCE"]
