"""Offline integration tests for the Modal.com serverless-GPU provider.

Modal serves vLLM's OpenAI-compatible API on serverless GPUs. APEX reaches it
like any other vLLM endpoint: it shapes as OpenAI, needs no API key by default
(an optional MODAL_INFERENCE_KEY bearer token can be armed), and its
``LLM_EXTRA_MODELS`` entries must produce usable ``ProviderSpec``s. Everything
here runs with zero network — the HTTP call is exercised through an injected
transport.
"""

import json
from types import SimpleNamespace

from llm.client import LLMClient, _shape_for
from llm.provider_credentials import requires_api_key, resolve_api_key
from llm.provider_registry import ProviderState, build_provider_registry


# The two Modal entries mirror the shipped .env roster (provider "vllm" — an
# OpenAI-compatible self-hosted shape — with a Modal *.modal.run base_url).
MODAL_EXTRA_MODELS = [
    {"name": "modal-mistral", "provider": "vllm",
     "model": "mistralai/Mistral-7B-Instruct-v0.3",
     "base_url": "https://acme--apex-trader-llm-mistral-serve.modal.run/v1",
     "tier": 2, "timeout_seconds": 60, "classes": ["deep"]},
    {"name": "modal-qwen", "provider": "vllm",
     "model": "Qwen/Qwen2.5-7B-Instruct",
     "base_url": "https://acme--apex-trader-llm-qwen-serve.modal.run/v1",
     "tier": 2, "timeout_seconds": 60, "classes": ["fast"]},
]


def test_modal_shapes_as_openai():
    # The explicit "modal" provider name resolves to the OpenAI shape. As a
    # recognised alias it shapes even without a base_url (unlike a wholly
    # unknown vendor); usability still requires a base_url — see the client
    # test below and LLMClient.usable.
    assert _shape_for("modal", "https://example.modal.run/v1") == "openai"
    assert _shape_for("modal", "") == "openai"


def test_modal_requires_no_api_key():
    assert requires_api_key("modal") is False


def test_resolve_api_key_finds_modal_inference_key():
    env = {"MODAL_INFERENCE_KEY": "mk-secret"}
    assert resolve_api_key("modal", "", env=env) == "mk-secret"
    # Fallback env var name is also honoured.
    assert resolve_api_key("modal", "", env={"MODAL_API_KEY": "mk-alt"}) == "mk-alt"
    # Explicit spec key still wins over the env var.
    assert resolve_api_key("modal", "mk-spec", env=env) == "mk-spec"


def test_modal_entries_produce_usable_specs():
    cfg = SimpleNamespace(
        enabled=True, provider="gemini", model="gemini-3.6-flash",
        api_key="g-key", base_url="", extra_models=MODAL_EXTRA_MODELS,
    )
    registry = build_provider_registry(cfg)
    names = {s.name for s in registry.specs}
    assert {"modal-mistral", "modal-qwen"} <= names

    # Keyless OpenAI-compatible endpoints with a base_url are AVAILABLE as-is.
    for name in ("modal-mistral", "modal-qwen"):
        spec = registry.get(name)
        assert spec is not None
        assert spec.reachable is True
        assert spec.usable_shape is True
        assert spec.requires_key is False
        assert spec.state is ProviderState.AVAILABLE


def test_modal_extra_models_parse_from_env_json():
    # The exact JSON shape stored in .env round-trips into usable specs.
    raw = json.dumps(MODAL_EXTRA_MODELS)
    cfg = SimpleNamespace(
        enabled=True, provider="gemini", model="gemini-3.6-flash",
        api_key="g-key", base_url="", extra_models=json.loads(raw),
    )
    registry = build_provider_registry(cfg)
    spec = registry.get("modal-qwen")
    assert spec is not None
    assert spec.model == "Qwen/Qwen2.5-7B-Instruct"
    assert int(spec.tier) == 2


def test_modal_http_call_shapes_openai_compatible():
    captured = {}

    def fake_transport(url, headers, body, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = json.loads(body.decode("utf-8"))
        reply = {"choices": [{"message": {"content": "buy"}}]}
        return 200, json.dumps(reply)

    client = LLMClient(
        provider="vllm", model="mistralai/Mistral-7B-Instruct-v0.3",
        api_key="mk-secret",
        base_url="https://acme--apex-trader-llm-mistral-serve.modal.run/v1",
        transport=fake_transport,
    )
    assert client.usable is True

    out = client.complete("system prompt", "user prompt")
    assert out == "buy"
    # OpenAI-compatible endpoint: POST /chat/completions, bearer auth, and the
    # standard chat payload (model + system/user messages).
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["headers"]["authorization"] == "Bearer mk-secret"
    assert captured["payload"]["model"] == "mistralai/Mistral-7B-Instruct-v0.3"
    roles = [m["role"] for m in captured["payload"]["messages"]]
    assert roles == ["system", "user"]
