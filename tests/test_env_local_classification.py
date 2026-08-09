"""GPU/Compute Constitution §8 — the LOCAL (Tier 3) roster in the live ``.env``
must be FULLY classified.

Every locally-served model must declare the compute classes it serves and its
context window, so the class-aware router can place the Brain's ``deep`` cognition
on a reasoning-grade local model while keeping fast generalists for low-latency
work. A local model added WITHOUT a class/context silently falls back to "serves
any class", which defeats compute-class routing — so that is a bug this locks out.
"""

import json
from pathlib import Path

import pytest

from llm.model_manager import spec_classes, spec_context_window
from llm.provider_tiers import ProviderTier, resolve_tier

_ENV = Path(__file__).resolve().parents[1] / ".env"


def _local_specs() -> list:
    if not _ENV.exists():
        pytest.skip("no .env in repo root")
    line = next((ln for ln in _ENV.read_text().split("\n")
                 if ln.startswith("LLM_EXTRA_MODELS=")), None)
    if line is None:
        pytest.skip("LLM_EXTRA_MODELS not set in .env")
    models = json.loads(line.partition("=")[2])
    return [m for m in models if isinstance(m, dict)
            and int(resolve_tier(m.get("provider"), m.get("tier"))) == int(ProviderTier.TIER_3)]


def test_env_ships_a_local_failsafe_roster():
    assert _local_specs(), "the live .env should ship a local (Tier 3) failsafe roster"


def test_every_local_model_is_fully_classified():
    for spec in _local_specs():
        name = spec.get("name") or spec.get("model")
        assert spec_classes(spec), f"local model {name!r} must declare compute classes"
        assert spec_context_window(spec) > 0, f"local model {name!r} must declare a context_window"


def test_local_roster_offers_a_deep_reasoner():
    # The Brain requests the "deep" class; at least one local model must serve it
    # so there is an offline, keyless deep-reasoning failsafe of last resort.
    deep = [s for s in _local_specs() if "deep" in spec_classes(s)]
    assert deep, "at least one local model must serve the 'deep' compute class"


def test_local_provider_names_resolve_to_tier3():
    for prov in ("ollama", "vllm", "nvidia_nim_self_hosted"):
        assert resolve_tier(prov) is ProviderTier.TIER_3
