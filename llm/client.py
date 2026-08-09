"""APEX TRADER — Provider-agnostic LLM client.

The reasoning subsystem must be able to talk to **either** a self-hosted model
(Ollama, vLLM, LM Studio, any OpenAI-compatible endpoint) **or** a commercial
API (OpenAI, Anthropic, Google Gemini) — chosen entirely at runtime from the
environment, never hardcoded. Set ``LLM_PROVIDER`` + ``LLM_API_KEY`` (and, for a
self-hosted or custom endpoint, ``LLM_BASE_URL``) and that provider is used.

Design principles (mirror the trading leaf modules):

* **No hardcoded provider.** The provider name comes from config/env. An unknown
  provider with a ``base_url`` is treated as an OpenAI-compatible endpoint (so
  most self-hosted servers work out of the box); an unknown provider with no
  ``base_url`` disables the client (fail-safe), it never guesses a vendor.
* **No third-party dependency.** All HTTP uses the standard library
  (``urllib.request``) so no SDK is required and the same client serves every
  provider. Network I/O is isolated to a single ``transport`` callable that can
  be injected in tests, so the request-shaping / response-parsing for every
  provider is verifiable offline with zero network.
* **Fail-safe.** Every call returns ``None`` on any error (bad status, timeout,
  malformed body) rather than raising — a reasoning fault must never break the
  caller.
* **Secret-safe.** The API key is never logged; status/repr expose only the
  provider name, model and whether a key/base_url is present.
"""

from __future__ import annotations

import json
import ssl
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

from loguru import logger

# A transport turns a prepared request into ``(status_code, response_text)``.
# Injectable so tests exercise every provider's request/response shaping with no
# network. The default (:func:`_urllib_transport`) uses the standard library.
Transport = Callable[[str, dict, bytes, float], "tuple[int, str]"]

# Provider-name normalisation. Anything not listed here is treated as an
# OpenAI-compatible endpoint *iff* a base_url is supplied (covers vLLM, LM
# Studio, Together, Groq, OpenRouter, and any future self-hosted server).
_OPENAI_ALIASES = frozenset({
    "openai", "openai_compatible", "openai-compatible",
    "vllm", "lmstudio", "lm_studio", "together", "groq", "openrouter",
    "agentrouter", "agent_router", "agent-router",
    "deepseek", "mistral", "self_hosted", "self-hosted", "local",
})
_ANTHROPIC_ALIASES = frozenset({"anthropic", "claude"})
_GEMINI_ALIASES = frozenset({"gemini", "google", "google_gemini"})
_OLLAMA_ALIASES = frozenset({"ollama"})
_AZURE_ALIASES = frozenset({"azure_openai", "azure"})

# Recognised-but-not-yet-implemented providers. These need an auth model the
# rest of this client cannot produce from a flat api_key string (Vertex AI
# authenticates via OAuth2 / service-account tokens, not a static key/URL
# pair), so they are refused explicitly rather than silently mis-shaped onto
# a similar-looking vendor (Part XVI: never guess a vendor's request shape).
_UNSUPPORTED_PROVIDERS = frozenset({"vertex"})

_DEFAULT_AZURE_API_VERSION = "2024-06-01"

# Providers that carry a sensible built-in default endpoint, so ``base_url`` is
# optional. EVERY other provider — gateways (AgentRouter, OpenRouter), Azure,
# and self-hosted servers (vLLM, LM Studio, …) — MUST supply ``base_url``: they
# have no single canonical host, and silently falling back to OpenAI's URL would
# send the operator's key to the wrong place. Those are marked base-required.
_NO_BASE_REQUIRED = (
    _ANTHROPIC_ALIASES | _GEMINI_ALIASES | _OLLAMA_ALIASES | frozenset({"openai"})
)

_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "ollama": "http://localhost:11434",
}


def _shape_for(provider: str, base_url: str) -> Optional[str]:
    """Resolve a provider name to a request *shape* (or None if unusable)."""
    p = (provider or "").strip().lower()
    if not p:
        return None
    if p in _UNSUPPORTED_PROVIDERS:
        # Recognised, but this client cannot build a correct request for it
        # yet — fail closed rather than reuse a lookalike vendor's shape.
        return None
    if p in _ANTHROPIC_ALIASES:
        return "anthropic"
    if p in _GEMINI_ALIASES:
        return "gemini"
    if p in _OLLAMA_ALIASES:
        return "ollama"
    if p in _AZURE_ALIASES:
        return "azure_openai"
    if p in _OPENAI_ALIASES:
        return "openai"
    # Unknown vendor: usable only as an OpenAI-compatible endpoint when the
    # operator has given us a base_url to POST to. Otherwise fail-safe (None).
    return "openai" if base_url else None


def _requires_base_url(provider: str) -> bool:
    """True when the provider has no built-in default endpoint (see _NO_BASE_REQUIRED)."""
    return (provider or "").strip().lower() not in _NO_BASE_REQUIRED


_SSL_CONTEXT: "Optional[ssl.SSLContext]" = None


def _https_context() -> "Optional[ssl.SSLContext]":
    """A cached TLS context that trusts the up-to-date ``certifi`` CA bundle.

    Windows Python ships a CA store that cannot verify some providers' cert
    chains (Cohere, DeepInfra → ``CERTIFICATE_VERIFY_FAILED``). Preferring
    ``certifi``'s bundle fixes that without disabling verification. Falls back
    to the stdlib default context when ``certifi`` is not installed, and never
    raises — a context build fault degrades to the interpreter default.
    """
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    try:
        import certifi
        _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 — certifi missing / any build fault
        try:
            _SSL_CONTEXT = ssl.create_default_context()
        except Exception:  # noqa: BLE001
            return None
    return _SSL_CONTEXT


def _urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> "tuple[int, str]":
    """Default transport — a single stdlib POST. Isolated for test injection."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    ctx = _https_context() if str(url).lower().startswith("https") else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return int(getattr(resp, "status", 200) or 200), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # non-2xx
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        return int(exc.code or 0), detail
    except Exception as exc:  # noqa: BLE001 — timeouts, DNS, connection resets
        return 0, f"{type(exc).__name__}: {exc}"


@dataclass
class LLMClient:
    """A minimal, provider-agnostic chat client. Fail-safe; secret-safe."""

    provider: str
    model: str
    api_key: str = ""
    base_url: str = ""
    # Azure OpenAI only: the REST api-version query param (e.g. "2024-06-01").
    # Ignored by every other shape. Falls back to _DEFAULT_AZURE_API_VERSION
    # when the azure_openai shape is used and this is left blank.
    api_version: str = ""
    timeout_seconds: float = 20.0
    max_tokens: int = 1024
    temperature: float = 0.2
    transport: Optional[Transport] = None

    def __post_init__(self) -> None:
        self._shape = _shape_for(self.provider, self.base_url)
        self._requires_base_url = _requires_base_url(self.provider)
        self._transport: Transport = self.transport or _urllib_transport
        # Edge-triggered availability logging. A rate-limited / quota-exhausted /
        # offline advisor is retried every cycle for every symbol, so logging its
        # failure each time floods the log. Track the last outcome so a failure is
        # announced only on the DOWN transition (or when the failure class
        # changes), stays silent while it keeps failing the same way, and a
        # recovery is announced once when it rejoins.
        self._log_lock = threading.Lock()
        self._last_outcome_ok: Optional[bool] = None  # None=unknown, True=ok, False=failing
        self._last_fail_key: str = ""
        self._suppressed_failures: int = 0

    @property
    def usable(self) -> bool:
        """True when the provider resolves to a known shape and is reachable.

        A gateway / self-hosted provider (AgentRouter, OpenRouter, vLLM, …) is
        only usable once ``base_url`` is supplied — it never silently defaults
        to a vendor endpoint.
        """
        if self._shape is None or not self.model:
            return False
        if self._requires_base_url and not self.base_url:
            return False
        return True

    @property
    def last_fail_signature(self) -> str:
        """The most recent failure signature (e.g. ``http:504``, ``http:429``,
        ``transport:TimeoutError``), or ``""`` when the last call succeeded.

        Read-only view used by the background recovery prober to decide how
        eagerly a benched provider should be retried.
        """
        return getattr(self, "_last_fail_key", "") or ""

    def _effective_base(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return _DEFAULT_BASE_URLS.get(self._shape or "", "").rstrip("/")

    def _note_failure(self, signature: str, message: str) -> None:
        """Log a provider failure only on the DOWN transition (edge-triggered).

        The first failure (or a change in failure class, e.g. HTTP 429 → 500) is
        logged at WARNING with the reason; while the provider keeps failing the
        same way it is logged at DEBUG only, so a persistently rate-limited /
        offline advisor cannot flood the log every cycle. The suppressed-repeat
        count is carried so recovery can report how long it was silent.
        """
        with self._log_lock:
            if self._last_outcome_ok is False and signature == self._last_fail_key:
                self._suppressed_failures += 1
                logger.debug(
                    "[llm] {} still unavailable ({}) — silently retried x{}",
                    self.provider, signature, self._suppressed_failures,
                )
                return
            self._last_outcome_ok = False
            self._last_fail_key = signature
            self._suppressed_failures = 0
        logger.warning("[llm] {} unavailable — {}", self.provider, message)

    def _note_success(self) -> None:
        """Log a one-line recovery only on the UP transition (edge-triggered)."""
        with self._log_lock:
            was_failing = self._last_outcome_ok is False
            suppressed = self._suppressed_failures
            self._last_outcome_ok = True
            self._last_fail_key = ""
            self._suppressed_failures = 0
        if was_failing:
            logger.info(
                "[llm] {} recovered — rejoined the council{}",
                self.provider,
                f" (silent through {suppressed} repeat failure(s))" if suppressed else "",
            )

    def complete(self, system: str, user: str) -> Optional[str]:
        """Return the model's text reply for a system+user prompt, or None.

        Never raises: any transport error, non-2xx status, or malformed body
        yields ``None`` so a reasoning fault cannot break the caller.
        """
        if not self.usable:
            return None
        try:
            url, headers, payload = self._build_request(system, user)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[llm] request build failed ({}): {}", self.provider, exc)
            return None
        # Python's default urllib User-Agent ("Python-urllib/3.x") is a known
        # bot-fingerprint that some vendors' Cloudflare front doors reject
        # outright (e.g. Groq: HTTP 403, "error code: 1010") even with a
        # perfectly valid key/payload. A normal-looking UA avoids that.
        headers.setdefault("user-agent", "apex-trader-llm-client/1.0")
        body = json.dumps(payload).encode("utf-8")
        try:
            status, text = self._transport(url, headers, body, self.timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            self._note_failure(
                f"transport:{type(exc).__name__}", f"transport error: {exc}"
            )
            return None
        if status < 200 or status >= 300:
            # Edge-triggered: log status + a short, key-free snippet once.
            self._note_failure(f"http:{status}", f"HTTP {status} — {(text or '')[:160]}")
            return None
        try:
            reply = self._parse_reply(text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[llm] reply parse failed ({}): {}", self.provider, exc)
            reply = None
        if reply:
            self._note_success()
        return reply

    # ── Per-provider request shaping ──────────────────────────────────────

    def _build_request(self, system: str, user: str) -> "tuple[str, dict, dict]":
        base = self._effective_base()
        shape = self._shape
        if shape == "anthropic":
            url = f"{base}/v1/messages"
            headers = {
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            }
            payload = {
                "model": self.model,
                "max_tokens": int(self.max_tokens),
                "temperature": float(self.temperature),
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            return url, headers, payload
        if shape == "gemini":
            # Gemini carries the key as a query param, not a bearer header.
            url = f"{base}/v1beta/models/{self.model}:generateContent?key={self.api_key}"
            headers = {"content-type": "application/json"}
            payload = {
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "systemInstruction": {"parts": [{"text": system}]},
                "generationConfig": {
                    "temperature": float(self.temperature),
                    "maxOutputTokens": int(self.max_tokens),
                },
            }
            return url, headers, payload
        if shape == "ollama":
            url = f"{base}/api/chat"
            headers = {"content-type": "application/json"}
            if self.api_key:  # some gateways front Ollama with auth
                headers["authorization"] = f"Bearer {self.api_key}"
            payload = {
                "model": self.model,
                "stream": False,
                "keep_alive": 0,
                "options": {"temperature": float(self.temperature)},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            return url, headers, payload
        if shape == "azure_openai":
            # Azure OpenAI: auth via `api-key` header (not Bearer), and the
            # model is selected by *deployment name* in the URL path, not a
            # `model` field in the payload. `base_url` must be the resource
            # endpoint, e.g. https://<resource>.openai.azure.com — `model`
            # here is treated as the deployment name (the common convention
            # of naming a deployment after the model it serves).
            version = self.api_version or _DEFAULT_AZURE_API_VERSION
            url = f"{base}/openai/deployments/{self.model}/chat/completions?api-version={version}"
            headers = {
                "content-type": "application/json",
                "api-key": self.api_key,
            }
            payload = {
                "temperature": float(self.temperature),
                "max_tokens": int(self.max_tokens),
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            return url, headers, payload
        # Default: OpenAI Chat Completions shape (also self-hosted compatible).
        url = f"{base}/chat/completions"
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "temperature": float(self.temperature),
            "max_tokens": int(self.max_tokens),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        return url, headers, payload

    # ── Per-provider response parsing ─────────────────────────────────────

    def _parse_reply(self, text: str) -> Optional[str]:
        data: Any = json.loads(text)
        shape = self._shape
        if shape == "anthropic":
            parts = data.get("content") or []
            for part in parts:
                if isinstance(part, dict) and part.get("type", "text") == "text":
                    return part.get("text")
            return None
        if shape == "gemini":
            cands = data.get("candidates") or []
            if not cands:
                return None
            parts = (cands[0].get("content") or {}).get("parts") or []
            texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
            return "".join(texts) or None
        if shape == "ollama":
            return (data.get("message") or {}).get("content")
        # OpenAI-compatible.
        choices = data.get("choices") or []
        if not choices:
            return None
        return (choices[0].get("message") or {}).get("content")

    def describe(self) -> dict:
        """Secret-safe description — never includes the API key."""
        return {
            "provider": self.provider,
            "shape": self._shape,
            "model": self.model,
            "base_url": self._effective_base(),
            "has_api_key": bool(self.api_key),
            "requires_base_url": self._requires_base_url,
            "usable": self.usable,
            "timeout_seconds": self.timeout_seconds,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }


def build_client(config: Any, transport: Optional[Transport] = None) -> Optional[LLMClient]:
    """Construct an :class:`LLMClient` from an ``LLMConfig``-like object.

    Returns ``None`` when no provider is configured or the provider cannot be
    resolved to a usable request shape — so an absent/mis-set env is a safe
    no-op, never an error.
    """
    provider = str(getattr(config, "provider", "") or "").strip()
    if not provider:
        return None
    client = LLMClient(
        provider=provider,
        model=str(getattr(config, "model", "") or ""),
        api_key=str(getattr(config, "api_key", "") or ""),
        base_url=str(getattr(config, "base_url", "") or ""),
        timeout_seconds=float(getattr(config, "timeout_seconds", 20.0) or 20.0),
        max_tokens=int(getattr(config, "max_tokens", 512) or 512),
        temperature=float(getattr(config, "temperature", 0.2) or 0.2),
        transport=transport,
    )
    if not client.usable:
        if client._requires_base_url and not client.base_url:
            logger.warning(
                "[llm] provider '{}' needs LLM_BASE_URL (gateway/self-hosted has "
                "no default endpoint) — reasoner disabled", provider,
            )
        else:
            logger.warning(
                "[llm] provider '{}' not usable (unknown provider and no "
                "LLM_BASE_URL, or no model set) — reasoner disabled", provider,
            )
        return None
    return client


__all__ = ["LLMClient", "Transport", "build_client"]
