"""Offline tests for provider tiers + tiered failover (Constitution Part XXIII)."""

from types import SimpleNamespace

from llm.provider_tiers import (
    ProviderTier,
    coerce_tier,
    resolve_tier,
    tier_for,
    tier_label,
)
from llm.provider_registry import build_provider_registry
from llm.model_manager import ModelManager, _Candidate, build_model_manager


# ── Classifier ────────────────────────────────────────────────────────────────

def test_tier_for_known_providers():
    assert tier_for("openai") is ProviderTier.TIER_1
    assert tier_for("anthropic") is ProviderTier.TIER_1
    assert tier_for("gemini") is ProviderTier.TIER_1
    assert tier_for("perplexity") is ProviderTier.TIER_1      # the live primary
    assert tier_for("groq") is ProviderTier.TIER_2
    assert tier_for("openrouter") is ProviderTier.TIER_2
    assert tier_for("ollama") is ProviderTier.TIER_3
    assert tier_for("vllm") is ProviderTier.TIER_3
    assert tier_for("nvidia") is ProviderTier.TIER_2               # NVIDIA NIM hosted
    assert tier_for("nvidia_nim") is ProviderTier.TIER_2
    assert tier_for("nvidia_nim_self_hosted") is ProviderTier.TIER_3  # NIM local
    assert tier_for("some-new-vendor") is ProviderTier.UNKNOWN
    assert tier_for("") is ProviderTier.UNKNOWN


def test_tier_ordering_values():
    # Lower value = preferred first in failover.
    assert ProviderTier.TIER_1 < ProviderTier.TIER_2 < ProviderTier.TIER_3 < ProviderTier.UNKNOWN


def test_coerce_and_resolve_override():
    assert coerce_tier(1) is ProviderTier.TIER_1
    assert coerce_tier("tier_2") is ProviderTier.TIER_2
    assert coerce_tier("t3") is ProviderTier.TIER_3
    assert coerce_tier(None) is None
    assert coerce_tier(9) is None
    # Override wins over name-based classification.
    assert resolve_tier("openai", 3) is ProviderTier.TIER_3
    assert resolve_tier("openai", None) is ProviderTier.TIER_1


def test_tier_label():
    assert tier_label(ProviderTier.TIER_1) == "tier_1"
    assert tier_label(ProviderTier.UNKNOWN) == "unknown"


# ── Provider Registry tier catalogue ──────────────────────────────────────────

def _cfg(**kw):
    base = dict(enabled=True, provider="openai", model="gpt-4o-mini",
                api_key="sk-test", base_url="", extra_models=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_registry_tags_and_counts_tiers():
    reg = build_provider_registry(_cfg(extra_models=[
        {"provider": "groq", "model": "llama-3.1-70b", "api_key": "gk"},
        {"provider": "ollama", "model": "llama3.1", "base_url": "http://localhost:11434"},
    ]))
    tiers = {s.provider: s.tier for s in reg.specs}
    assert tiers["openai"] is ProviderTier.TIER_1
    assert tiers["groq"] is ProviderTier.TIER_2
    assert tiers["ollama"] is ProviderTier.TIER_3
    st = reg.get_status()
    assert st["tiers"]["tier_1"] == 1
    assert st["tiers"]["tier_2"] == 1
    assert st["tiers"]["tier_3"] == 1
    assert st["providers"][0]["tier"] == 1  # to_dict carries the tier


def test_registry_explicit_tier_override():
    reg = build_provider_registry(_cfg(extra_models=[
        {"provider": "openai", "model": "gpt-x", "tier": 3},   # pin a T1 provider to T3
    ]))
    got = {s.model: s.tier for s in reg.specs}
    assert got["gpt-x"] is ProviderTier.TIER_3


# ── ModelManager tiered failover (Art 14) ─────────────────────────────────────

class _FakeClient:
    def __init__(self, provider, model, reply="ok", usable=True):
        self.provider = provider
        self.model = model
        self._reply = reply
        self.usable = usable

    def complete(self, system, user):
        return self._reply


def test_ordered_tier_leads_over_priority():
    # A Tier-1 candidate with a WORSE operator priority still leads a Tier-2 one.
    t2 = _Candidate(client=_FakeClient("groq", "m2"), priority=0, tier=2)
    t1 = _Candidate(client=_FakeClient("openai", "m1"), priority=9, tier=1)
    mgr = ModelManager([t2, t1])
    ordered = mgr._ordered()
    assert [c.model for c in ordered] == ["m1", "m2"]   # Tier 1 first


def test_ordered_preserves_priority_within_same_tier():
    a = _Candidate(client=_FakeClient("groq", "a"), priority=0, tier=2)
    b = _Candidate(client=_FakeClient("openrouter", "b"), priority=1, tier=2)
    mgr = ModelManager([b, a])
    assert [c.model for c in mgr._ordered()] == ["a", "b"]   # priority within tier


def test_untiered_setup_is_behavior_preserving():
    # All UNKNOWN tier (e.g. exotic providers) ⇒ pure priority order, as before.
    x = _Candidate(client=_FakeClient("x", "x"), priority=0, tier=int(ProviderTier.UNKNOWN))
    y = _Candidate(client=_FakeClient("y", "y"), priority=1, tier=int(ProviderTier.UNKNOWN))
    mgr = ModelManager([y, x])
    assert [c.model for c in mgr._ordered()] == ["x", "y"]


def test_failover_from_dead_tier1_to_tier2():
    # Tier-1 out of credits (returns None) ⇒ fail over to Tier-2.
    dead_t1 = _Candidate(client=_FakeClient("openai", "m1", reply=None), priority=0, tier=1)
    live_t2 = _Candidate(client=_FakeClient("groq", "m2", reply="hello"), priority=1, tier=2)
    mgr = ModelManager([dead_t1, live_t2])
    reply = mgr.complete("sys", "user")
    assert reply == "hello"
    assert mgr.describe()["selected_model"] == "m2"
    assert mgr.describe()["failovers"] >= 1


def test_build_model_manager_assigns_tiers():
    mgr = build_model_manager(_cfg(
        provider="perplexity", model="sonar",
        api_key="pk", base_url="https://api.perplexity.ai",
        extra_models=[{"provider": "ollama", "model": "llama3.1",
                       "base_url": "http://localhost:11434"}],
    ))
    assert mgr is not None
    cands = {c["model"]: c for c in mgr.describe()["candidates"]}
    assert cands["sonar"]["tier"] == 1            # perplexity ⇒ Tier 1
    assert cands["llama3.1"]["tier"] == 3         # ollama ⇒ Tier 3
