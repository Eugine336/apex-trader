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
import re
import ssl
import threading
import time as _time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

from loguru import logger

# A transport turns a prepared request into ``(status_code, response_text)`` or,
# optionally, ``(status_code, response_text, response_headers)`` — the default
# transport returns the 3-tuple so a vendor-signalled cooldown (Retry-After /
# rate-limit reset) can be honoured, while any injected 2-tuple transport
# (tests / custom) keeps working unchanged. Injectable so tests exercise every
# provider's request/response shaping with no network. The default
# (:func:`_urllib_transport`) uses the standard library.
Transport = Callable[[str, dict, bytes, float], "tuple"]

# Provider-name normalisation. Anything not listed here is treated as an
# OpenAI-compatible endpoint *iff* a base_url is supplied (covers vLLM, LM
# Studio, Together, Groq, OpenRouter, and any future self-hosted server).
_OPENAI_ALIASES = frozenset({
    "openai", "openai_compatible", "openai-compatible",
    "vllm", "lmstudio", "lm_studio", "together", "groq", "openrouter",
    "agentrouter", "agent_router", "agent-router",
    "deepseek", "mistral", "self_hosted", "self-hosted", "local",
    # Modal.com serverless GPUs serve vLLM's OpenAI-compatible API, so the
    # provider name "modal" shapes exactly like any other vLLM endpoint.
    "modal",
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


def _headers_to_dict(headers: Any) -> dict:
    """Normalise an HTTP header container to a lowercase-keyed dict. Fail-safe."""
    out: dict = {}
    if headers is None:
        return out
    try:
        items = headers.items()
    except Exception:  # noqa: BLE001 — not a mapping / message object
        return out
    for k, v in items:
        try:
            out[str(k).strip().lower()] = v
        except Exception:  # noqa: BLE001
            continue
    return out


def _parse_duration_token(value: Any) -> float:
    """Parse a rate-limit reset value to seconds. Fail-safe (0.0 on anything odd).

    Accepts a bare number of seconds (``"45"``, ``2.5``) or a compound duration
    string as commonly emitted by OpenAI/Groq-style ``x-ratelimit-reset*``
    headers (``"6m0s"``, ``"1m30s"``, ``"500ms"``, ``"1h2m3s"``)."""
    s = str(value).strip().lower()
    if not s:
        return 0.0
    try:
        return max(0.0, float(s))          # bare number ⇒ seconds
    except ValueError:
        pass
    units = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    total = 0.0
    matched = False
    # ``ms`` must be tried before ``s`` — the alternation order handles that.
    for num, unit in re.findall(r"([0-9]*\.?[0-9]+)\s*(ms|s|m|h|d)", s):
        try:
            total += float(num) * units[unit]
            matched = True
        except (ValueError, KeyError):
            continue
    return total if matched else 0.0


def _retry_after_value(raw: Any) -> float:
    """Parse a ``Retry-After`` header (delta-seconds or an HTTP-date) to seconds
    from now. Fail-safe — returns 0.0 for anything unparsable."""
    s = str(raw).strip()
    if not s:
        return 0.0
    try:
        return max(0.0, float(s))          # RFC 7231 delta-seconds
    except ValueError:
        pass
    try:  # RFC 7231 HTTP-date form
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt is None:
            return 0.0
        return max(0.0, dt.timestamp() - _time.time())
    except Exception:  # noqa: BLE001 — malformed date
        return 0.0


def _parse_retry_after(headers: dict) -> float:
    """Extract the largest vendor-signalled cooldown (seconds) from response
    headers: RFC ``Retry-After`` plus common ``x-ratelimit-reset*`` variants.
    Fail-safe — returns 0.0 when nothing usable is present."""
    if not isinstance(headers, dict) or not headers:
        return 0.0
    best = 0.0
    ra = headers.get("retry-after")
    if ra is not None:
        best = max(best, _retry_after_value(ra))
    for key in (
        "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens",
        "x-ratelimit-reset", "ratelimit-reset",
    ):
        v = headers.get(key)
        if v is not None:
            best = max(best, _parse_duration_token(v))
    return best if best > 0.0 else 0.0


def _extract_usage(text: str, shape: Optional[str]) -> int:
    """Total tokens a provider reported for a reply, or 0. Fail-safe.

    Covers the shapes this client speaks: OpenAI-compatible/Azure ``usage``,
    Anthropic ``usage.input_tokens+output_tokens``, Gemini
    ``usageMetadata.totalTokenCount``, and Ollama eval counts."""
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001 — malformed / empty body
        return 0
    if not isinstance(data, dict):
        return 0
    try:
        if shape == "anthropic":
            u = data.get("usage") or {}
            return int(u.get("input_tokens", 0) or 0) + int(u.get("output_tokens", 0) or 0)
        if shape == "gemini":
            u = data.get("usageMetadata") or {}
            return int(u.get("totalTokenCount", 0) or 0)
        if shape == "ollama":
            return int(data.get("prompt_eval_count", 0) or 0) + int(data.get("eval_count", 0) or 0)
        # OpenAI-compatible / Azure.
        u = data.get("usage") or {}
        total = u.get("total_tokens")
        if total is not None:
            return int(total or 0)
        return int(u.get("prompt_tokens", 0) or 0) + int(u.get("completion_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> "tuple[int, str, dict]":
    """Default transport — a single stdlib POST. Isolated for test injection.

    Returns ``(status, text, response_headers)``; the header dict lets the
    client honour a vendor-signalled cooldown (Retry-After / rate-limit reset).
    """
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    ctx = _https_context() if str(url).lower().startswith("https") else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            text = resp.read().decode("utf-8", "replace")
            return status, text, _headers_to_dict(getattr(resp, "headers", None))
    except urllib.error.HTTPError as exc:  # non-2xx
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        return int(exc.code or 0), detail, _headers_to_dict(getattr(exc, "headers", None))
    except Exception as exc:  # noqa: BLE001 — timeouts, DNS, connection resets
        return 0, f"{type(exc).__name__}: {exc}", {}


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
    # GPU/Compute Constitution §22–§28 — optional per-provider quota meter
    # (:class:`llm.provider_budget.ProviderBudget`). When attached (an operator
    # configured real free-tier limits) the client refuses to send a request that
    # would exceed the provider's remaining RPM/RPD/TPM/TPD and benches it until
    # the window frees, instead of blindly burning a scarce quota. ``None``
    # (default) ⇒ unmetered, behaviour unchanged.
    budget: Optional[Any] = None

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
        # Most recent vendor-signaled cooldown in seconds (HTTP 429 Retry-After
        # / rate-limit reset header), or 0.0 when the last call did not signal
        # one. Read by the council so a quota-exhausted provider is benched until
        # capacity returns instead of being re-hammered every cycle (§27).
        self._last_retry_after: float = 0.0
        # Token usage parsed from the most recent successful reply (provider
        # ``usage`` / ``usageMetadata`` / eval counts), or 0. Feeds the budget
        # meter and is exposed for observability (§34).
        self._last_usage_tokens: int = 0

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

    @property
    def last_retry_after_seconds(self) -> float:
        """Seconds the provider asked us to wait after the most recent call
        (from an HTTP 429 ``Retry-After`` or rate-limit reset header), or
        ``0.0`` when none was signalled / the last call succeeded.

        Lets the council bench a rate-limited provider until its quota actually
        resets, instead of retrying it every cycle and deepening the throttle
        (Constitution §27 — quota recovery). Fail-safe read."""
        try:
            return max(0.0, float(getattr(self, "_last_retry_after", 0.0) or 0.0))
        except (TypeError, ValueError):
            return 0.0

    @property
    def last_usage_tokens(self) -> int:
        """Total tokens the most recent successful call consumed (provider-
        reported when available, else 0). Exposed for quota observability (§34).
        Fail-safe read."""
        try:
            return max(0, int(getattr(self, "_last_usage_tokens", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _record_budget(self, tokens: int) -> None:
        """Log one SENT request against the attached quota meter (if any). A
        metering fault must never break a reasoning call."""
        b = self.budget
        if b is None:
            return
        try:
            b.record(tokens)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[llm] budget record fault ({}): {}", self.provider, exc)

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
        # §28 — never blindly exhaust a scarce free tier. When a quota meter is
        # attached and this request would exceed the provider's remaining
        # capacity, refuse to send it and bench the provider until the window
        # frees (reusing the same cooldown path as an HTTP 429), so the council
        # routes elsewhere instead of burning the quota into a hard rate-limit.
        est_tokens = 0
        budget = self.budget
        if budget is not None:
            try:
                from llm.provider_budget import estimate_tokens
                est_tokens = estimate_tokens(system, user, self.max_tokens)
                if not budget.admits(est_tokens):
                    self._last_retry_after = float(budget.blocked_until(est_tokens) or 0.0)
                    self._last_usage_tokens = 0
                    self._note_failure(
                        "budget:exhausted",
                        f"local quota guard — benched ~{self._last_retry_after:.0f}s",
                    )
                    return None
            except Exception as exc:  # noqa: BLE001 — a metering fault must not block a call
                logger.debug("[llm] budget guard fault ({}): {}", self.provider, exc)
                est_tokens = 0
        try:
            result = self._transport(url, headers, body, self.timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            self._last_retry_after = 0.0
            self._last_usage_tokens = 0
            self._note_failure(
                f"transport:{type(exc).__name__}", f"transport error: {exc}"
            )
            return None
        # A transport may return (status, text) or (status, text, headers). The
        # 3-tuple lets us honour a vendor-signalled cooldown; a 2-tuple (custom /
        # test transport) is still fully supported.
        resp_headers: dict = {}
        try:
            if isinstance(result, tuple) and len(result) >= 3:
                status, text, resp_headers = int(result[0]), result[1], (result[2] or {})
            else:
                status, text = result  # 2-tuple transport
        except Exception as exc:  # noqa: BLE001 — a malformed transport return
            self._last_retry_after = 0.0
            self._last_usage_tokens = 0
            self._note_failure(
                f"transport:{type(exc).__name__}", f"malformed transport return: {exc}"
            )
            return None
        if status < 200 or status >= 300:
            # Capture any vendor-signalled cooldown (Retry-After / rate-limit
            # reset) BEFORE logging, so the council can bench this provider until
            # its quota returns instead of re-hammering it next cycle (§27).
            self._last_retry_after = _parse_retry_after(resp_headers)
            self._last_usage_tokens = 0
            # The request WAS sent — it counts against the provider's RPM/RPD.
            self._record_budget(0)
            # Edge-triggered: log status + a short, key-free snippet once.
            self._note_failure(f"http:{status}", f"HTTP {status} — {(text or '')[:160]}")
            return None
        # 2xx — clear any stale cooldown carried from a prior throttled call.
        self._last_retry_after = 0.0
        used = _extract_usage(text, self._shape)
        self._last_usage_tokens = used if used > 0 else est_tokens
        self._record_budget(self._last_usage_tokens)
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
            "budget": self._budget_snapshot(),
        }

    def _budget_snapshot(self) -> Optional[dict]:
        """Fail-safe view of the attached quota meter (None when unmetered)."""
        b = self.budget
        if b is None:
            return None
        try:
            return b.to_dict()
        except Exception:  # noqa: BLE001
            return None


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
    # §22–§28 — attach a quota meter when the operator configured real free-tier
    # limits (rpm/rpd/tpm/tpd). All default 0 ⇒ unmetered, behaviour unchanged;
    # limits come from config (the provider's real service), never baked in.
    def _limit(attr: str) -> int:
        try:
            return max(0, int(getattr(config, attr, 0) or 0))
        except (TypeError, ValueError):
            return 0
    rpm, rpd = _limit("rpm_limit"), _limit("rpd_limit")
    tpm, tpd = _limit("tpm_limit"), _limit("tpd_limit")
    if rpm or rpd or tpm or tpd:
        try:
            from llm.provider_budget import ProviderBudget
            client.budget = ProviderBudget(rpm=rpm, rpd=rpd, tpm=tpm, tpd=tpd)
            logger.info(
                "[llm] provider '{}' quota meter on (rpm={} rpd={} tpm={} tpd={})",
                provider, rpm, rpd, tpm, tpd,
            )
        except Exception as exc:  # noqa: BLE001 — a budget wiring fault must not disable the client
            logger.debug("[llm] budget wiring fault ({}): {}", provider, exc)
    return client


__all__ = ["LLMClient", "Transport", "build_client"]
