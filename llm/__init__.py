"""APEX TRADER — LLM reasoning subsystem (provider-agnostic).

Public surface:

* :class:`llm.client.LLMClient` / :func:`llm.client.build_client` — the
  provider-agnostic chat client (OpenAI / Anthropic / Gemini / Ollama / any
  self-hosted OpenAI-compatible endpoint), selected from config/env.
* :class:`llm.reasoner.LLMReasoner` / :class:`llm.reasoner.LLMOpinion` — turns
  structured market evidence into a structured, fail-safe opinion emitted as
  evidence for the consensus panel.
"""

from llm.client import LLMClient, Transport, build_client
from llm.model_manager import (
    POLICY_PERFORMANCE,
    POLICY_PRIORITY,
    ModelManager,
    build_model_manager,
)
from llm.reasoning_orchestrator import (
    EngineOpinion,
    ReasoningConsultation,
    ReasoningEngine,
    ReasoningOrchestrator,
    build_reasoning_orchestrator,
)
from llm.reasoner import FLAT, LONG, SHORT, LLMOpinion, LLMReasoner
from llm.provider_registry import (
    ProviderRegistry,
    ProviderSpec,
    ProviderState,
    build_provider_registry,
)
from llm.worker import EvidenceSource, LLMReasoningWorker

__all__ = [
    "LLMClient",
    "Transport",
    "build_client",
    "ModelManager",
    "build_model_manager",
    "POLICY_PRIORITY",
    "POLICY_PERFORMANCE",
    "ReasoningOrchestrator",
    "ReasoningEngine",
    "ReasoningConsultation",
    "EngineOpinion",
    "build_reasoning_orchestrator",
    "ProviderRegistry",
    "ProviderSpec",
    "ProviderState",
    "build_provider_registry",
    "LLMReasoner",
    "LLMOpinion",
    "LLMReasoningWorker",
    "EvidenceSource",
    "LONG",
    "SHORT",
    "FLAT",
]
