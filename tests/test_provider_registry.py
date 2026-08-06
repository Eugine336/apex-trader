"""Offline tests for the Provider Registry / Manager (Constitution Part XXI)."""

from types import SimpleNamespace

from llm.provider_registry import (
    ProviderRegistry,
    ProviderSpec,
    ProviderState,
    build_provider_registry,
)


# ── ProviderSpec.state resolution (Article 5) ─────────────────────────────────

def test_available_when_shape_usable_and_authenticated():
    # openai has a built-in endpoint; a key authenticates it → AVAILABLE.
    spec = ProviderSpec(
        name="p", provider="openai", model="gpt-4o-mini",
        authenticated=True, reachable=False, usable_shape=True,
    )
    assert spec.state is ProviderState.AVAILABLE
    assert spec.can_activate is False


def test_configured_when_awaiting_credentials():
    # Shape is fine but no key and no base_url → integrated, awaiting creds.
    spec = ProviderSpec(
        name="p", provider="openai", model="gpt-4o-mini",
        authenticated=False, reachable=False, usable_shape=True,
    )
    assert spec.state is ProviderState.CONFIGURED
    assert spec.can_activate is True


def test_configured_when_gateway_awaiting_base_url():
    # A gateway with a key but no base_url cannot build a request yet.
    spec = ProviderSpec(
        name="g", provider="openrouter", model="x",
        authenticated=True, reachable=False, usable_shape=False,
    )
    assert spec.state is ProviderState.CONFIGURED


def test_unavailable_when_explicitly_disabled():
    spec = ProviderSpec(
        name="p", provider="openai", model="gpt-4o-mini",
        authenticated=True, reachable=False, usable_shape=True,
        explicitly_disabled=True,
    )
    assert spec.state is ProviderState.UNAVAILABLE


def test_state_override_pins_state():
    spec = ProviderSpec(
        name="p", provider="openai", model="gpt-4o-mini",
        authenticated=True, reachable=False, usable_shape=True,
        state_override="unavailable",
    )
    assert spec.state is ProviderState.UNAVAILABLE


def test_to_dict_is_secret_safe():
    spec = ProviderSpec(
        name="p", provider="openai", model="m", authenticated=True,
        usable_shape=True, capabilities=("risk",),
    )
    d = spec.to_dict()
    assert d["state"] == "available"
    assert d["authenticated"] is True
    assert d["capabilities"] == ["risk"]
    # Never leak the key material — only the boolean.
    assert "api_key" not in d and "key" not in d


# ── build_provider_registry from a config-like object ─────────────────────────

def _cfg(**kw):
    base = dict(enabled=True, provider="openai", model="gpt-4o-mini",
                api_key="sk-test", base_url="", extra_models=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_primary_available_and_flagged():
    reg = build_provider_registry(_cfg())
    assert reg.subsystem_enabled is True
    assert len(reg) == 1
    prim = reg.specs[0]
    assert prim.is_primary is True
    assert prim.state is ProviderState.AVAILABLE


def test_build_extra_models_inherit_key_and_classify():
    reg = build_provider_registry(_cfg(extra_models=[
        {"provider": "anthropic", "model": "claude-3-5-sonnet"},   # inherits key → AVAILABLE
        {"provider": "openrouter", "model": "z"},                  # needs base_url → CONFIGURED
        {"provider": "openai", "model": "gpt-x", "enabled": False},  # excluded → UNAVAILABLE
    ]))
    assert len(reg) == 4  # primary + 3
    states = {s.model: s.state for s in reg.specs}
    assert states["claude-3-5-sonnet"] is ProviderState.AVAILABLE
    assert states["z"] is ProviderState.CONFIGURED
    assert states["gpt-x"] is ProviderState.UNAVAILABLE


def test_build_configured_when_primary_has_no_credentials():
    reg = build_provider_registry(_cfg(api_key="", base_url=""))
    assert reg.specs[0].state is ProviderState.CONFIGURED


def test_gateway_available_with_base_url_and_key():
    reg = build_provider_registry(_cfg(
        provider="perplexity", model="sonar",
        api_key="pk", base_url="https://api.perplexity.ai",
    ))
    assert reg.specs[0].state is ProviderState.AVAILABLE


def test_incomplete_specs_are_skipped():
    reg = build_provider_registry(_cfg(extra_models=[
        {"provider": "openai"},          # no model → skipped
        {"model": "orphan-no-provider", "provider": ""},  # no provider → skipped
    ]))
    assert len(reg) == 1  # only the primary


def test_duplicate_names_deduped():
    reg = build_provider_registry(_cfg(extra_models=[
        {"name": "gpt-4o-mini", "provider": "openai", "model": "gpt-4o-mini"},
    ]))
    assert len(reg) == 1


def test_get_status_counts_and_grouping():
    reg = build_provider_registry(_cfg(extra_models=[
        {"provider": "anthropic", "model": "claude"},
        {"provider": "openrouter", "model": "z"},
    ]))
    st = reg.get_status()
    assert st["total"] == 3
    assert st["subsystem_enabled"] is True
    assert st["counts"]["available"] == 2      # openai primary + anthropic
    assert st["counts"]["configured"] == 1     # openrouter (no base_url)
    assert len(reg.available) == 2 and len(reg.configured) == 1
    assert reg.get("z").model == "z"


def test_build_is_fail_safe_on_garbage_config():
    reg = build_provider_registry(object())  # no attributes at all
    assert isinstance(reg, ProviderRegistry)
    assert len(reg) == 0
    assert reg.get_status()["total"] == 0
