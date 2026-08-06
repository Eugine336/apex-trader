"""APEX TRADER — Provider credential resolution (Constitution Part XXIII Art 15/16).

Part XXIII: every provider is architecturally integrated from day one; activation
requires *only credentials* — "financial growth activates providers, it never
redesigns Apex." That means the operator should be able to pre-wire the whole
provider roster in ``.env`` and simply drop a key in to light one up, while a
*blank* entry stays inert and never affects the others.

This module makes that true. It resolves each provider's API key from a
conventional per-provider environment variable (``OPENAI_API_KEY``,
``GROQ_API_KEY``, ``OPENROUTER_API_KEY``, …) and answers whether a provider even
needs a key (local runtimes like Ollama / vLLM do not). The builders use it to
**skip** any credential-requiring provider that has no key — so a blank roster
entry is catalogued as CONFIGURED (awaiting credentials) but is never selected,
never called, and never fails a live request.

Crucially it also fixes a footgun: an extra model that omits its key previously
inherited the *primary* provider's key, which for a different vendor would ship
one vendor's secret to another's endpoint. Here the primary key/base_url are
inherited ONLY for the same provider (the genuine "one gateway, many models"
case); a different vendor resolves its own ``<PROVIDER>_API_KEY`` or stays blank.

Pure standard library; env-injectable for tests; never raises.
"""

from __future__ import annotations

import os
import re
from typing import Optional

# Providers served by a local runtime need no API key (authenticate by locality).
_KEYLESS_PROVIDERS = frozenset({
    "ollama", "vllm", "lmstudio", "lm_studio", "local", "self_hosted",
    "self-hosted", "llamacpp", "llama_cpp", "llamafile", "gpt4all",
    "koboldcpp", "textgen", "text_generation_webui",
})

# Explicit env-var names per provider (first non-empty wins). Anything not listed
# falls back to the generic ``<PROVIDER>_API_KEY`` form.
_KEY_ENV: dict = {
    "openai": ("OPENAI_API_KEY",),
    "azure_openai": ("AZURE_OPENAI_API_KEY", "OPENAI_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "claude": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "google_gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "vertex": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "grok": ("XAI_API_KEY", "GROK_API_KEY"),
    "xai": ("XAI_API_KEY", "GROK_API_KEY"),
    "x-ai": ("XAI_API_KEY", "GROK_API_KEY"),
    "x_ai": ("XAI_API_KEY", "GROK_API_KEY"),
    "cohere": ("COHERE_API_KEY",),
    "perplexity": ("PERPLEXITY_API_KEY", "PPLX_API_KEY"),
    "pplx": ("PERPLEXITY_API_KEY", "PPLX_API_KEY"),
    "groq": ("GROQ_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "together": ("TOGETHER_API_KEY", "TOGETHERAI_API_KEY"),
    "togetherai": ("TOGETHER_API_KEY", "TOGETHERAI_API_KEY"),
    "together_ai": ("TOGETHER_API_KEY", "TOGETHERAI_API_KEY"),
    "fireworks": ("FIREWORKS_API_KEY",),
    "fireworks_ai": ("FIREWORKS_API_KEY",),
    "fireworksai": ("FIREWORKS_API_KEY",),
    "deepinfra": ("DEEPINFRA_API_KEY",),
    "cerebras": ("CEREBRAS_API_KEY",),
    "huggingface": ("HUGGINGFACE_API_KEY", "HF_TOKEN"),
    "hugging_face": ("HUGGINGFACE_API_KEY", "HF_TOKEN"),
    "hf": ("HUGGINGFACE_API_KEY", "HF_TOKEN"),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "anyscale": ("ANYSCALE_API_KEY",),
    "replicate": ("REPLICATE_API_TOKEN", "REPLICATE_API_KEY"),
}


def _norm(provider) -> str:
    try:
        return str(provider or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def requires_api_key(provider) -> bool:
    """False for local runtimes (they authenticate by locality); True otherwise."""
    return _norm(provider) not in _KEYLESS_PROVIDERS


def key_env_names(provider) -> tuple:
    """Environment variable name(s) that hold this provider's key (best first)."""
    p = _norm(provider)
    if not p:
        return ()
    if p in _KEY_ENV:
        return _KEY_ENV[p]
    generic = re.sub(r"[^A-Z0-9]+", "_", p.upper()).strip("_")
    return (f"{generic}_API_KEY",) if generic else ()


def resolve_api_key(
    provider,
    spec_key: str = "",
    *,
    primary_provider: str = "",
    primary_key: str = "",
    env: Optional[dict] = None,
) -> str:
    """Resolve a provider's key: explicit spec key → ``<PROVIDER>_API_KEY`` env →
    the primary key ONLY when the provider matches the primary (same vendor)."""
    env = env if env is not None else os.environ
    key = str(spec_key or "").strip()
    if key:
        return key
    for name in key_env_names(provider):
        try:
            v = str(env.get(name) or "").strip()
        except Exception:  # noqa: BLE001
            v = ""
        if v:
            return v
    if _norm(provider) and _norm(provider) == _norm(primary_provider):
        return str(primary_key or "").strip()
    return ""


def resolve_base_url(
    provider,
    spec_base_url: str = "",
    *,
    primary_provider: str = "",
    primary_base: str = "",
) -> str:
    """Resolve a base_url: explicit spec value, else inherit the primary's base
    ONLY for the same provider (never point one vendor at another's endpoint)."""
    base = str(spec_base_url or "").strip()
    if base:
        return base
    if _norm(provider) and _norm(provider) == _norm(primary_provider):
        return str(primary_base or "").strip()
    return ""


def has_credentials(provider, api_key: str) -> bool:
    """True when a provider is credential-ready: a key is present, or it needs
    none (local runtime). A credential-requiring provider with a blank key is
    NOT ready — the caller should skip it so a blank roster entry stays inert."""
    return bool(str(api_key or "").strip()) or not requires_api_key(provider)


__all__ = [
    "requires_api_key",
    "key_env_names",
    "resolve_api_key",
    "resolve_base_url",
    "has_credentials",
]
