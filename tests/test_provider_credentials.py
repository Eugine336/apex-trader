"""Offline tests for per-provider credential resolution (Part XXIII Art 15)."""

import os
from types import SimpleNamespace

from llm.provider_credentials import (
    has_credentials,
    key_env_names,
    requires_api_key,
    resolve_api_key,
    resolve_base_url,
)
from llm.model_manager import build_model_manager


def test_requires_api_key():
    assert requires_api_key("openai") is True
    assert requires_api_key("groq") is True
    assert requires_api_key("ollama") is False        # local runtime
    assert requires_api_key("vllm") is False


def test_key_env_names():
    assert key_env_names("openai") == ("OPENAI_API_KEY",)
    assert "OPENROUTER_API_KEY" in key_env_names("openrouter")
    assert "GEMINI_API_KEY" in key_env_names("gemini")
    # Unknown provider → generic <PROVIDER>_API_KEY form.
    assert key_env_names("acme-labs") == ("ACME_LABS_API_KEY",)


def test_resolve_api_key_precedence():
    env = {"GROQ_API_KEY": "gk-env"}
    # 1) explicit spec key wins
    assert resolve_api_key("groq", "gk-spec", env=env) == "gk-spec"
    # 2) per-provider env var
    assert resolve_api_key("groq", "", env=env) == "gk-env"
    # 3) same-vendor inherit of the primary key
    assert resolve_api_key("perplexity", "", primary_provider="perplexity",
                           primary_key="pk", env={}) == "pk"
    # 4) DIFFERENT vendor never inherits the primary key → blank
    assert resolve_api_key("openai", "", primary_provider="perplexity",
                           primary_key="pk", env={}) == ""


def test_resolve_base_url_same_vendor_only():
    assert resolve_base_url("perplexity", "", primary_provider="perplexity",
                            primary_base="https://api.perplexity.ai") == "https://api.perplexity.ai"
    assert resolve_base_url("openai", "", primary_provider="perplexity",
                            primary_base="https://api.perplexity.ai") == ""
    assert resolve_base_url("groq", "https://api.groq.com/openai/v1") == "https://api.groq.com/openai/v1"


def test_has_credentials():
    assert has_credentials("openai", "sk-x") is True
    assert has_credentials("openai", "") is False       # key required, none ⇒ inert
    assert has_credentials("ollama", "") is True         # local ⇒ ready without a key


def _cfg(**kw):
    base = dict(enabled=True, provider="perplexity", model="sonar",
                api_key="pk", base_url="https://api.perplexity.ai", extra_models=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_blank_roster_entry_is_inert():
    # A pre-wired but keyless different-vendor entry must NOT become a candidate.
    mgr = build_model_manager(_cfg(extra_models=[
        {"provider": "openai", "model": "gpt-4o-mini"},           # no key
        {"provider": "groq", "model": "llama-3.3-70b",
         "base_url": "https://api.groq.com/openai/v1"},           # no key
    ]))
    assert mgr is not None
    models = {c["model"] for c in mgr.describe()["candidates"]}
    assert models == {"sonar"}   # only the credentialed primary — blanks inert


def test_filling_env_key_activates_provider():
    os.environ.pop("GROQ_API_KEY", None)
    try:
        os.environ["GROQ_API_KEY"] = "gk-live"
        mgr = build_model_manager(_cfg(extra_models=[
            {"provider": "groq", "model": "llama-3.3-70b",
             "base_url": "https://api.groq.com/openai/v1"},
        ]))
        models = {c["model"] for c in mgr.describe()["candidates"]}
        assert "llama-3.3-70b" in models   # a filled key lights the provider up
    finally:
        os.environ.pop("GROQ_API_KEY", None)


def test_local_provider_needs_no_key():
    mgr = build_model_manager(_cfg(extra_models=[
        {"provider": "ollama", "model": "llama3.1", "base_url": "http://localhost:11434"},
    ]))
    models = {c["model"] for c in mgr.describe()["candidates"]}
    assert "llama3.1" in models   # keyless local runtime is ready as-is
