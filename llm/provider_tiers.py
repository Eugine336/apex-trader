"""APEX TRADER — Provider tier taxonomy (Constitution Part XXIII Art 5/6/7/14).

Part XXIII organises every reasoning provider into three permanent tiers and
mandates tiered failover between them:

* **Tier 1 — Frontier commercial.** Highest-quality institutional reasoning
  (OpenAI, Anthropic Claude, Google Gemini, xAI Grok, Cohere, Perplexity).
* **Tier 2 — Hosted open-source.** Fast, low-cost, scalable hosted OSS models
  and gateways (Groq, OpenRouter, Together, Fireworks, DeepInfra, Cerebras,
  Hugging Face, and provider-agnostic gateways).
* **Tier 3 — Local institutional.** Permanently owned, offline, zero-API
  reasoning (anything served by a local runtime — Ollama, vLLM, LM Studio,
  self-hosted — plus locally-run model families Llama/Qwen/Mistral/…).

This module is the single, pure classifier the rest of the ecosystem shares so
the Provider Registry (state + tier catalogue) and the Model Manager (tiered
failover) always agree on which tier a provider belongs to. Classification keys
on the *provider name* (what the config actually selects); an operator can
always override per model via an explicit ``tier`` field. A provider that
matches nothing is ``UNKNOWN`` (sorts after the known tiers, so a recognised
frontier/hosted model is preferred over a mystery one — never the reverse).

Pure standard library; never raises.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Optional


class ProviderTier(IntEnum):
    """Provider tiers (lower value = preferred first in tiered failover)."""

    TIER_1 = 1          # frontier commercial
    TIER_2 = 2          # hosted open-source / gateways
    TIER_3 = 3          # local institutional
    UNKNOWN = 4         # unclassified — tried after every known tier


# Provider-name → tier. Names are lowercased. Follows the Part XXIII taxonomy and
# this codebase's provider aliases (see llm/client.py). Note: "deepseek" and
# "mistral" as *provider names* mean their hosted cloud APIs here (Tier 2); when
# those model families run under a local runtime the provider is the runtime
# (ollama/vllm/…) and resolves to Tier 3, which is the intended behaviour.
_TIER_1 = frozenset({
    "openai", "azure_openai", "anthropic", "claude", "gemini", "google",
    "google_gemini", "vertex", "grok", "xai", "x-ai", "x_ai", "cohere",
    "perplexity", "pplx", "perplexity_ai",
})
_TIER_2 = frozenset({
    "groq", "openrouter", "together", "togetherai", "together_ai",
    "fireworks", "fireworks_ai", "fireworksai", "deepinfra", "cerebras",
    "huggingface", "hugging_face", "hf", "anyscale", "replicate",
    "agentrouter", "agent_router", "agent-router", "deepseek", "mistral",
})
_TIER_3 = frozenset({
    "ollama", "local", "self_hosted", "self-hosted", "vllm", "lmstudio",
    "lm_studio", "llamacpp", "llama_cpp", "llamafile", "gpt4all",
    "koboldcpp", "textgen", "text_generation_webui",
    # locally-run model families used directly as a provider name
    "llama", "qwen", "gemma", "phi",
})

_LABELS = {
    ProviderTier.TIER_1: "tier_1",
    ProviderTier.TIER_2: "tier_2",
    ProviderTier.TIER_3: "tier_3",
    ProviderTier.UNKNOWN: "unknown",
}


def tier_for(provider: Any) -> ProviderTier:
    """Classify a provider name into its :class:`ProviderTier` (never raises)."""
    try:
        p = str(provider or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ProviderTier.UNKNOWN
    if not p:
        return ProviderTier.UNKNOWN
    if p in _TIER_1:
        return ProviderTier.TIER_1
    if p in _TIER_2:
        return ProviderTier.TIER_2
    if p in _TIER_3:
        return ProviderTier.TIER_3
    return ProviderTier.UNKNOWN


def coerce_tier(value: Any) -> Optional[ProviderTier]:
    """Parse an explicit operator tier override (1/2/3, or a label) → tier.

    Accepts ``1``/``2``/``3``, ``"tier_1"``/``"tier1"``/``"t1"`` and plain
    ``"1"``. Returns ``None`` when the value is absent/unrecognised so the
    caller can fall back to name-based classification.
    """
    if value is None or value == "":
        return None
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            n = int(value)
            return ProviderTier(n) if 1 <= n <= 3 else None
        s = str(value).strip().lower().replace("-", "_")
        for digit, tier in (("1", ProviderTier.TIER_1), ("2", ProviderTier.TIER_2),
                            ("3", ProviderTier.TIER_3)):
            if s in (digit, f"tier_{digit}", f"tier{digit}", f"t{digit}"):
                return tier
    except Exception:  # noqa: BLE001
        return None
    return None


def resolve_tier(provider: Any, override: Any = None) -> ProviderTier:
    """Explicit override wins; otherwise classify by provider name."""
    forced = coerce_tier(override)
    return forced if forced is not None else tier_for(provider)


def tier_label(tier: ProviderTier) -> str:
    return _LABELS.get(tier, "unknown")


__all__ = ["ProviderTier", "tier_for", "coerce_tier", "resolve_tier", "tier_label"]
