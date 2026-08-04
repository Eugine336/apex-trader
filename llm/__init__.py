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
from llm.reasoner import FLAT, LONG, SHORT, LLMOpinion, LLMReasoner
from llm.worker import EvidenceSource, LLMReasoningWorker

__all__ = [
    "LLMClient",
    "Transport",
    "build_client",
    "LLMReasoner",
    "LLMOpinion",
    "LLMReasoningWorker",
    "EvidenceSource",
    "LONG",
    "SHORT",
    "FLAT",
]
