"""APEX TRADER — Provider Registry & Manager (Constitution Part XXI).

Part XXI mandates a Provider Manager that maintains *every* reasoning provider
and the three constitutional provider states (Article 5) — without ever
requiring an architectural change to move a provider between them (Article 5,
16, 17). This module is that catalogue:

* :class:`ProviderState` — the three permanent states:
    - ``AVAILABLE``    configured, authenticated/reachable, ready to reason.
    - ``CONFIGURED``   integrated and architecture-complete, merely awaiting
                       credentials (or a base_url / enable) — it can become
                       AVAILABLE the instant a key is supplied, no redesign.
    - ``UNAVAILABLE``  offline, disabled, failed or explicitly excluded.
* :class:`ProviderSpec` — one known advisor (name, provider, model, capability
  tags, and whether it is authenticated / reachable / shape-usable) with a
  derived :pyattr:`~ProviderSpec.state`.
* :class:`ProviderRegistry` — the manager: holds every spec, groups by state,
  and reports a secret-safe status. It owns *catalogue* lifecycle only; it never
  reasons, selects for a query, or talks to the Brain (that is the Reasoning
  Orchestrator's job, Part XVII / Part XXI Art 2). The Brain remains the sole
  decider.

Provider readiness is derived from the same rules the live client uses
(:class:`llm.client.LLMClient.usable`) so the catalogue never disagrees with
what would actually run. Pure standard library and fail-safe throughout: a bad
spec is skipped or classified conservatively, never raised. Secrets (API keys)
are represented only as a boolean ``authenticated`` and are never stored beyond
the classification moment nor emitted in any status.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from loguru import logger

from llm.provider_tiers import ProviderTier, resolve_tier, tier_label


class ProviderState(str, Enum):
    """The three constitutional provider states (Part XXI, Article 5)."""

    AVAILABLE = "available"
    CONFIGURED = "configured"
    UNAVAILABLE = "unavailable"


def _coerce_state(value: Any) -> Optional[ProviderState]:
    """Best-effort parse of a state override string; None if unrecognised."""
    s = str(value or "").strip().lower()
    for st in ProviderState:
        if s == st.value:
            return st
    return None


@dataclass
class ProviderSpec:
    """One reasoning provider in the catalogue with a derived constitutional state.

    ``authenticated`` = an API key is present; ``reachable`` = a base_url is
    present (a keyless self-hosted / gateway endpoint authenticates by URL);
    ``usable_shape`` = the provider resolves to a request shape the client can
    actually build (Part XVI abstraction — unknown vendors need a base_url).
    ``explicitly_disabled`` marks an operator-excluded provider. An optional
    ``state_override`` lets an operator or future health monitor pin the state.
    """

    name: str
    provider: str
    model: str
    capabilities: tuple = ()
    authenticated: bool = False
    reachable: bool = False
    usable_shape: bool = False
    explicitly_disabled: bool = False
    is_primary: bool = False
    state_override: str = ""
    tier: ProviderTier = ProviderTier.UNKNOWN

    @property
    def state(self) -> ProviderState:
        forced = _coerce_state(self.state_override)
        if forced is not None:
            return forced
        if self.explicitly_disabled:
            return ProviderState.UNAVAILABLE
        # AVAILABLE requires a runnable shape AND a way to authenticate/reach it.
        if self.usable_shape and (self.authenticated or self.reachable):
            return ProviderState.AVAILABLE
        # Integrated but awaiting credentials / base_url / correct config — it
        # can activate with no architectural change (Art 5 / 16 / 17).
        return ProviderState.CONFIGURED

    @property
    def can_activate(self) -> bool:
        """True when only credentials/config stand between here and AVAILABLE."""
        return self.state is ProviderState.CONFIGURED

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "capabilities": list(self.capabilities),
            "state": self.state.value,
            "tier": int(self.tier),
            "tier_label": tier_label(self.tier),
            "authenticated": bool(self.authenticated),   # boolean only — never the key
            "reachable": bool(self.reachable),
            "is_primary": bool(self.is_primary),
        }


class ProviderRegistry:
    """The Provider Manager's catalogue of every reasoning provider (Part XXI).

    Maintains provider *identity and state*; it never reasons or selects for a
    market query. ``subsystem_enabled`` records whether the reasoning subsystem
    is globally switched on — a provider may be AVAILABLE (has creds) while the
    subsystem is off, which is exactly the "ready to activate" picture Article 17
    describes. Fail-safe; secret-safe.
    """

    def __init__(
        self,
        specs: Optional[list] = None,
        *,
        subsystem_enabled: bool = False,
    ) -> None:
        self._specs: list = [s for s in (specs or []) if isinstance(s, ProviderSpec)]
        self.subsystem_enabled = bool(subsystem_enabled)

    def __len__(self) -> int:
        return len(self._specs)

    @property
    def specs(self) -> list:
        return list(self._specs)

    def by_state(self, state: ProviderState) -> list:
        return [s for s in self._specs if s.state is state]

    @property
    def available(self) -> list:
        return self.by_state(ProviderState.AVAILABLE)

    @property
    def configured(self) -> list:
        return self.by_state(ProviderState.CONFIGURED)

    @property
    def unavailable(self) -> list:
        return self.by_state(ProviderState.UNAVAILABLE)

    def by_tier(self, tier: ProviderTier) -> list:
        return [s for s in self._specs if s.tier is tier]

    def get(self, name: str) -> Optional[ProviderSpec]:
        key = str(name or "")
        for s in self._specs:
            if s.name == key:
                return s
        return None

    def get_status(self) -> dict:
        """A secret-safe status of the whole provider catalogue."""
        counts = {st.value: 0 for st in ProviderState}
        tiers: dict = {}
        for s in self._specs:
            counts[s.state.value] += 1
            tiers[tier_label(s.tier)] = tiers.get(tier_label(s.tier), 0) + 1
        return {
            "subsystem_enabled": self.subsystem_enabled,
            "total": len(self._specs),
            "counts": counts,
            "tiers": tiers,
            "providers": [s.to_dict() for s in self._specs],
        }


def _usable_shape(provider: str, model: str, api_key: str, base_url: str) -> bool:
    """Whether the live client could build a request for this provider.

    Reuses :class:`llm.client.LLMClient.usable` so the catalogue agrees with
    what would actually run. Falls back to a minimal (provider+model) check if
    the client cannot be imported for any reason.
    """
    try:
        from llm.client import LLMClient
        return bool(LLMClient(
            provider=str(provider or ""), model=str(model or ""),
            api_key=str(api_key or ""), base_url=str(base_url or ""),
        ).usable)
    except Exception:  # noqa: BLE001 — never let classification raise
        return bool(provider) and bool(model)


def _spec_from(
    name: str, provider: str, model: str, api_key: str, base_url: str,
    capabilities: Any, *, is_primary: bool, disabled: bool, state_override: str,
    tier_override: Any = None,
) -> Optional[ProviderSpec]:
    provider = str(provider or "").strip()
    model = str(model or "").strip()
    if not provider or not model:
        return None  # not an integrable provider — skip silently
    caps = tuple(
        str(c).strip().lower() for c in (capabilities or []) if str(c).strip()
    )
    return ProviderSpec(
        name=str(name or model or provider),
        provider=provider, model=model, capabilities=caps,
        authenticated=bool(str(api_key or "").strip()),
        reachable=bool(str(base_url or "").strip()),
        usable_shape=_usable_shape(provider, model, api_key, base_url),
        explicitly_disabled=bool(disabled),
        is_primary=bool(is_primary),
        state_override=str(state_override or ""),
        tier=resolve_tier(provider, tier_override),
    )


def build_provider_registry(config: Any) -> ProviderRegistry:
    """Build a :class:`ProviderRegistry` from an ``LLMConfig``-like object.

    Catalogues the primary model plus every ``extra_models`` entry (Part XVI /
    XXI). Extra specs inherit the primary key/base_url when omitted (the "one
    gateway, many models" case), matching the Model Manager / Orchestrator
    builders. Each extra spec may carry ``capabilities`` (Art 6), and may be
    excluded with ``enabled: false`` / ``disabled: true`` or pinned with
    ``state``. Never raises — returns an empty registry on any fault.
    """
    try:
        primary_provider = str(getattr(config, "provider", "") or "")
        primary_model = str(getattr(config, "model", "") or "")
        primary_key = str(getattr(config, "api_key", "") or "")
        primary_base = str(getattr(config, "base_url", "") or "")
        subsystem_enabled = bool(getattr(config, "enabled", False))

        specs: list = []
        seen: set = set()

        def _add(spec: Optional[ProviderSpec]) -> None:
            if spec is not None and spec.name not in seen:
                specs.append(spec)
                seen.add(spec.name)

        _add(_spec_from(
            primary_model or primary_provider or "primary",
            primary_provider, primary_model, primary_key, primary_base,
            [], is_primary=True, disabled=False, state_override="",
        ))

        extra = getattr(config, "extra_models", None)
        if isinstance(extra, list):
            for spec in extra:
                if not isinstance(spec, dict):
                    continue
                name = str(
                    spec.get("name") or spec.get("model")
                    or spec.get("provider") or "engine"
                )
                # ``enabled: false`` or ``disabled: true`` excludes a provider.
                disabled = (spec.get("enabled") is False) or bool(spec.get("disabled"))
                _add(_spec_from(
                    name,
                    spec.get("provider", primary_provider),
                    spec.get("model", ""),
                    spec.get("api_key", primary_key),
                    spec.get("base_url", primary_base),
                    spec.get("capabilities", []),
                    is_primary=False, disabled=disabled,
                    state_override=str(spec.get("state", "") or ""),
                    tier_override=spec.get("tier"),
                ))

        return ProviderRegistry(specs, subsystem_enabled=subsystem_enabled)
    except Exception as exc:  # noqa: BLE001 — catalogue build must never break boot
        logger.debug("[provider-registry] build failed: {}", exc)
        return ProviderRegistry([], subsystem_enabled=False)


__all__ = [
    "ProviderState",
    "ProviderSpec",
    "ProviderRegistry",
    "build_provider_registry",
]
