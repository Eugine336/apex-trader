"""Offline tests for the provider-agnostic LLM reasoning subsystem.

Every provider's request shaping and response parsing is exercised with an
injected fake transport — zero network. The reasoner is tested with a stub
client so its prompt/parse/throttle/fail-safe behaviour is fully covered.
"""

import json

import pytest

from llm.client import LLMClient, build_client
from llm.reasoner import LLMOpinion, LLMReasoner


# ── Fake transport ───────────────────────────────────────────────────────────

def _transport(status, response_obj):
    calls = []

    def _t(url, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": json.loads(body.decode())})
        return status, json.dumps(response_obj)

    _t.calls = calls
    return _t


# ── Provider resolution ──────────────────────────────────────────────────────

def test_build_client_none_without_provider():
    class Cfg:
        provider = ""
        model = "m"
    assert build_client(Cfg()) is None


def test_unknown_provider_without_base_url_is_unusable():
    c = LLMClient(provider="acme", model="m")
    assert c.usable is False


def test_unknown_provider_with_base_url_is_openai_compatible():
    c = LLMClient(provider="acme", model="m", base_url="http://host:8000/v1")
    assert c.usable is True
    assert c._shape == "openai"


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini", "ollama"])
def test_known_providers_usable(provider):
    assert LLMClient(provider=provider, model="m", api_key="k").usable is True


# ── Per-provider request shaping + response parsing ──────────────────────────

def test_openai_shape_and_parse():
    t = _transport(200, {"choices": [{"message": {"content": "hello"}}]})
    c = LLMClient(provider="openai", model="gpt-x", api_key="secret", transport=t)
    out = c.complete("sys", "usr")
    assert out == "hello"
    call = t.calls[0]
    assert call["url"].endswith("/chat/completions")
    assert call["headers"]["authorization"] == "Bearer secret"
    assert call["body"]["model"] == "gpt-x"


def test_self_hosted_openai_compatible_uses_base_url():
    t = _transport(200, {"choices": [{"message": {"content": "ok"}}]})
    c = LLMClient(provider="vllm", model="llama3.1", base_url="http://gpu:8000/v1", transport=t)
    assert c.complete("s", "u") == "ok"
    assert t.calls[0]["url"] == "http://gpu:8000/v1/chat/completions"


def test_anthropic_shape_and_parse():
    t = _transport(200, {"content": [{"type": "text", "text": "claude-says"}]})
    c = LLMClient(provider="anthropic", model="claude-x", api_key="ak", transport=t)
    assert c.complete("s", "u") == "claude-says"
    call = t.calls[0]
    assert call["url"].endswith("/v1/messages")
    assert call["headers"]["x-api-key"] == "ak"
    assert call["headers"]["anthropic-version"]
    assert call["body"]["system"] == "s"


def test_gemini_shape_and_parse():
    t = _transport(200, {"candidates": [{"content": {"parts": [{"text": "gem"}]}}]})
    c = LLMClient(provider="gemini", model="gemini-x", api_key="gk", transport=t)
    assert c.complete("s", "u") == "gem"
    assert ":generateContent?key=gk" in t.calls[0]["url"]


def test_ollama_shape_and_parse():
    t = _transport(200, {"message": {"content": "local-model"}})
    c = LLMClient(provider="ollama", model="llama3.1", base_url="http://localhost:11434", transport=t)
    assert c.complete("s", "u") == "local-model"
    assert t.calls[0]["url"].endswith("/api/chat")


def test_non_2xx_returns_none():
    t = _transport(500, {"error": "boom"})
    c = LLMClient(provider="openai", model="m", api_key="k", transport=t)
    assert c.complete("s", "u") is None


def test_transport_exception_is_fail_safe():
    def _boom(url, headers, body, timeout):
        raise RuntimeError("network down")

    c = LLMClient(provider="openai", model="m", api_key="k", transport=_boom)
    assert c.complete("s", "u") is None


def test_describe_never_leaks_key():
    c = LLMClient(provider="openai", model="m", api_key="topsecret")
    d = c.describe()
    assert "topsecret" not in json.dumps(d)
    assert d["has_api_key"] is True


# ── Reasoner ─────────────────────────────────────────────────────────────────

class _StubClient:
    usable = True
    model = "stub"

    def __init__(self, reply):
        self._reply = reply

    def complete(self, system, user):
        self._last_user = user
        return self._reply

    def describe(self):
        return {"provider": "stub", "model": self.model, "has_api_key": False}


def test_reasoner_unavailable_when_disabled():
    r = LLMReasoner(client=_StubClient("{}"), enabled=False)
    assert r.available is False
    assert r.reason("EURUSD", {"x": 1}) is None


def test_reasoner_unavailable_without_client():
    r = LLMReasoner(client=None, enabled=True)
    assert r.available is False
    assert r.reason("EURUSD", {}) is None


def test_reasoner_parses_strict_json():
    reply = json.dumps({
        "direction": "long", "confidence": 0.72,
        "rationale": "HTF trend up", "competing_hypotheses": ["range"],
        "missing_information": ["news"],
    })
    r = LLMReasoner(client=_StubClient(reply), enabled=True, min_interval_seconds=0)
    op = r.reason("EURUSD", {"thesis": "x"})
    assert isinstance(op, LLMOpinion)
    assert op.direction == "LONG"
    assert op.confidence == pytest.approx(0.72)
    assert op.competing_hypotheses == ["range"]


def test_reasoner_tolerates_prose_wrapped_json():
    reply = "Sure!\n```json\n{\"direction\":\"SHORT\",\"confidence\":2}\n```\ndone"
    r = LLMReasoner(client=_StubClient(reply), enabled=True, min_interval_seconds=0)
    op = r.reason("EURUSD", {})
    assert op.direction == "SHORT"
    assert op.confidence == 1.0  # clamped


def test_reasoner_junk_reply_returns_none():
    r = LLMReasoner(client=_StubClient("no json here"), enabled=True, min_interval_seconds=0)
    assert r.reason("EURUSD", {}) is None


def test_reasoner_throttle():
    reply = json.dumps({"direction": "FLAT", "confidence": 0.1})
    r = LLMReasoner(client=_StubClient(reply), enabled=True, min_interval_seconds=100.0)
    assert r.reason("EURUSD", {}, now=1000.0) is not None
    assert r.reason("EURUSD", {}, now=1000.5) is None       # throttled
    assert r.reason("EURUSD", {}, now=1200.0) is not None   # past interval


def test_opinion_as_evidence_shape():
    op = LLMOpinion(symbol="EURUSD", direction="LONG", confidence=0.6)
    ev = op.as_evidence(weight=2.0)
    assert ev["module"] == "llm_reasoner"
    assert ev["direction"] == "LONG"
    assert ev["confidence"] == pytest.approx(0.6)
    assert ev["weight"] == pytest.approx(2.0)


def test_reasoner_status_is_secret_safe():
    r = LLMReasoner(client=_StubClient("{}"), enabled=True)
    status = r.get_status()
    assert status["enabled"] is True
    assert "recent_opinions" in status
    assert "topsecret" not in json.dumps(status)
