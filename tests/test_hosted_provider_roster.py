"""Hosted GPU inference clouds (Cloudflare Workers AI, GMI Cloud) — roster wiring.

These are Tier-2 (hosted open-source) providers that serve open models over
OpenAI-compatible APIs, offloading the crash-prone local Tier-3 runtime. They
must classify as Tier 2, resolve their own <PROVIDER>_API_KEY, arm from env
alone, and — for Cloudflare, whose endpoint embeds an account id — fill that id
into the base_url from ``CLOUDFLARE_ACCOUNT_ID`` with NO JSON edit. A missing
account id must leave the entry inert (never a malformed URL).
"""

import json
from pathlib import Path
from types import SimpleNamespace

from llm.model_manager import build_model_manager
from llm.provider_credentials import (
    key_env_names,
    requires_api_key,
    resolve_base_url,
)
from llm.provider_tiers import ProviderTier, resolve_tier

_ENV = Path(__file__).resolve().parents[1] / ".env"
_CF_TMPL = "https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/v1"


# ── Classification ──────────────────────────────────────────────────────────

def test_hosted_clouds_classify_as_tier2():
    for name in ("cloudflare", "cf", "workers_ai", "cloudflare_workers_ai",
                 "gmi", "gmi_cloud", "gmicloud"):
        assert resolve_tier(name) is ProviderTier.TIER_2, name


def test_hosted_clouds_require_a_key_with_conventional_env_names():
    assert requires_api_key("cloudflare") and requires_api_key("gmi")
    assert "CLOUDFLARE_API_KEY" in key_env_names("cloudflare")
    assert "GMI_API_KEY" in key_env_names("gmi")


# ── base_url env interpolation (Cloudflare account id) ──────────────────────

def test_base_url_fills_account_id_from_env():
    got = resolve_base_url("cloudflare", _CF_TMPL, env={"CLOUDFLARE_ACCOUNT_ID": "acct123"})
    assert got == "https://api.cloudflare.com/client/v4/accounts/acct123/ai/v1"


def test_base_url_blank_when_placeholder_unresolved():
    # No account id ⇒ endpoint not armed ⇒ inert (never a malformed URL).
    assert resolve_base_url("cloudflare", _CF_TMPL, env={}) == ""


def test_base_url_without_placeholder_is_unchanged():
    plain = "https://api.gmi-serving.com/v1"
    assert resolve_base_url("gmi", plain, env={}) == plain


# ── Live .env roster invariant ──────────────────────────────────────────────

def _roster():
    line = next((ln for ln in _ENV.read_text().split("\n")
                 if ln.startswith("LLM_EXTRA_MODELS=")), None)
    assert line is not None, "LLM_EXTRA_MODELS must be present in .env"
    return {m.get("name"): m for m in json.loads(line.partition("=")[2]) if isinstance(m, dict)}


def test_env_ships_cloudflare_and_gmi_tier2_entries():
    roster = _roster()
    for name in ("cloudflare", "gmi"):
        spec = roster.get(name)
        assert spec is not None, f"{name} must be in the .env roster"
        assert int(resolve_tier(spec.get("provider"), spec.get("tier"))) == 2
        assert spec.get("classes"), f"{name} must declare compute classes"
        assert int(spec.get("context_window", 0)) > 0, f"{name} must declare a context_window"


# ── End-to-end: armed vs inert ───────────────────────────────────────────────

def _cfg(specs):
    return SimpleNamespace(
        provider="gemini", model="g", api_key="x", base_url="",
        timeout_seconds=20.0, max_tokens=512, temperature=0.2,
        model_policy="performance", extra_models=specs,
    )


def test_hosted_clouds_arm_when_keyed(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_KEY", "cf-key")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct999")
    monkeypatch.setenv("GMI_API_KEY", "gmi-key")
    roster = _roster()
    mgr = build_model_manager(_cfg([roster["cloudflare"], roster["gmi"]]))
    cands = {c["provider"]: c for c in mgr.describe()["candidates"]}
    assert cands["cloudflare"]["usable"] and cands["cloudflare"]["tier"] == 2
    assert cands["gmi"]["usable"] and cands["gmi"]["tier"] == 2


def test_cloudflare_inert_without_account_id(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_KEY", "cf-key")
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    roster = _roster()
    mgr = build_model_manager(_cfg([roster["cloudflare"]]))
    cf = {c["provider"]: c for c in mgr.describe()["candidates"]}.get("cloudflare")
    # Present but not usable: the key armed it, but the missing account id blanks
    # the base_url, so it is never selected (fail-safe, like a blank key).
    assert cf is not None and not cf["usable"]
