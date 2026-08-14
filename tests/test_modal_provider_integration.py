"""Offline integration tests for the Modal.com managed inference provider.

Modal managed inference endpoints serve an OpenAI-compatible API. APEX reaches
the Qwen managed endpoint with provider ``modal``, the exact served model name,
and a ``.modal.direct`` ``/v1`` base URL. Endpoints may be unauthenticated, or
they may require a bearer token through ``MODAL_INFERENCE_KEY``/``api_key``.
Everything here runs with zero network: the HTTP call is exercised through an
injected transport.
"""

import json
from types import SimpleNamespace

from llm.client import LLMClient, _shape_for
from llm.provider_credentials import requires_api_key, resolve_api_key
from llm.provider_registry import ProviderState, build_provider_registry


MODAL_MODEL = "Qwen/Qwen3.6-35B-A3B"
MODAL_BASE_URL = (
    "https://eugine336--ep-qwen3-6-35b-a3b-server.ap-south.modal.direct/v1"
)
MODAL_TIMEOUT_SECONDS = 300


# Mirrors the managed SGLang endpoint configured from Modal's dashboard. The
# model string must exactly match the endpoint's --served-model-name.
MODAL_EXTRA_MODELS = [
    {
        "name": "modal-qwen35b",
        "provider": "modal",
        "model": MODAL_MODEL,
        "base_url": MODAL_BASE_URL,
        "api_key": "mk-secret",
        "tier": 2,
        "timeout_seconds": MODAL_TIMEOUT_SECONDS,
        "classes": ["deep"],
    },
]


def test_modal_shapes_as_openai():
    # The explicit "modal" provider name resolves to the OpenAI shape. As a
    # recognised alias it shapes even without a base_url (unlike a wholly
    # unknown vendor); usability still requires a base_url — see the client
    # test below and LLMClient.usable.
    assert _shape_for("modal", MODAL_BASE_URL) == "openai"
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
    assert {"modal-qwen35b"} <= names

    # Managed OpenAI-compatible endpoints with base_url and bearer auth are
    # AVAILABLE. The provider remains globally key-optional because Modal also
    # supports unauthenticated endpoints.
    for name in ("modal-qwen35b",):
        spec = registry.get(name)
        assert spec is not None
        assert spec.provider == "modal"
        assert spec.model == MODAL_MODEL
        assert spec.authenticated is True
        assert spec.reachable is True
        assert spec.usable_shape is True
        assert spec.requires_key is False
        assert spec.state is ProviderState.AVAILABLE


def test_modal_extra_models_parse_from_env_json():
    # The exact JSON shape stored in .env round-trips into usable specs.
    raw = json.dumps(MODAL_EXTRA_MODELS)
    round_tripped = json.loads(raw)
    cfg = SimpleNamespace(
        enabled=True, provider="gemini", model="gemini-3.6-flash",
        api_key="g-key", base_url="", extra_models=round_tripped,
    )
    registry = build_provider_registry(cfg)
    spec = registry.get("modal-qwen35b")
    assert spec is not None
    assert spec.model == MODAL_MODEL
    assert int(spec.tier) == 2
    assert round_tripped[0]["timeout_seconds"] == MODAL_TIMEOUT_SECONDS


def test_modal_http_call_shapes_openai_compatible():
    captured = {}

    def fake_transport(url, headers, body, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = json.loads(body.decode("utf-8"))
        captured["timeout"] = timeout
        reply = {"choices": [{"message": {"content": "buy"}}]}
        return 200, json.dumps(reply)

    client = LLMClient(
        provider="modal",
        model=MODAL_MODEL,
        api_key="mk-secret",
        base_url=MODAL_BASE_URL,
        timeout_seconds=MODAL_TIMEOUT_SECONDS,
        transport=fake_transport,
    )
    assert client.usable is True

    out = client.complete("system prompt", "user prompt")
    assert out == "buy"
    # OpenAI-compatible endpoint: POST /chat/completions, bearer auth, and the
    # standard chat payload (model + system/user messages).
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["headers"]["authorization"] == "Bearer mk-secret"
    assert captured["payload"]["model"] == MODAL_MODEL
    assert captured["timeout"] == MODAL_TIMEOUT_SECONDS
    roles = [m["role"] for m in captured["payload"]["messages"]]
    assert roles == ["system", "user"]
