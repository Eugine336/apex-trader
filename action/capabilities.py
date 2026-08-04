"""APEX TRADER — Capability registry (Constitution Part IX, Articles 3, 10, 11).

Composio exposes *capabilities*, not software. The AI reasons only in objectives
(Article 2); the Action Planner (Article 11) maps an objective's semantic
capability to a concrete **provider** and the Composio *action* that provider
performs. This module is that curated, typed capability layer — the fixed
allow-list the Brain may draw upon. It contains no reasoning and no market
opinion; it is an inert lookup table with a deterministic provider-selection
rule.

Each :class:`Capability` is a semantic verb (e.g. ``operator.notify``,
``github.create_issue``) carrying a risk tier, its required parameters, and an
ordered list of :class:`ProviderBinding` candidates — each binding a provider
name (``slack``, ``telegram``, ``github``, ``notion``, …) to the exact Composio
action string that realises the capability on that provider. Provider selection
(Article 11: "choose the appropriate provider") is a pure function of the
capability's candidates, an optional operator preference, and the set of
currently-available providers.

Pure standard library (``logging``); the RiskTier vocabulary is shared with the
orchestrator so governance reads one risk scale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from action.orchestrator import RiskTier, tier_from

logger = logging.getLogger("apex.action.capabilities")


class CapabilityCategory(Enum):
    """The constitutional kinds of capability (Articles 8 vs 9 vs 10)."""

    KNOWLEDGE_RETRIEVAL = "knowledge_retrieval"   # Article 4/8 — read → becomes Evidence
    OPERATIONAL = "operational"                   # Article 9 — create/update in the ecosystem
    NOTIFICATION = "notification"                 # Article 10 — operator coordination
    NOOP = "noop"                                 # deliberate do-nothing

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class ProviderBinding:
    """Binds one provider to the exact Composio action that realises a capability."""

    provider: str          # e.g. "slack", "telegram", "github", "notion", "googledrive"
    action: str            # the Composio action string, e.g. "GITHUB_CREATE_AN_ISSUE"

    def to_dict(self) -> dict:
        return {"provider": self.provider, "action": self.action}


@dataclass(frozen=True)
class Capability:
    """A semantic verb the Brain may target — never a provider or an API."""

    name: str
    category: CapabilityCategory
    risk_tier: RiskTier
    providers: tuple                      # tuple[ProviderBinding], ordered by preference
    required_params: tuple = ()           # param names that MUST be present
    reversible: bool = True
    required_permissions: tuple = ()
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category.value,
            "risk_tier": self.risk_tier.value,
            "providers": [p.to_dict() for p in self.providers],
            "required_params": list(self.required_params),
            "reversible": self.reversible,
            "required_permissions": list(self.required_permissions),
            "description": self.description,
        }

    def missing_params(self, params: dict) -> list:
        """Required params absent (or blank) in ``params``."""
        out = []
        for key in self.required_params:
            v = (params or {}).get(key)
            if v is None or (isinstance(v, str) and not v.strip()):
                out.append(key)
        return out


class CapabilityRegistry:
    """The curated allow-list of capabilities + deterministic provider selection.

    Fail-safe reads: an unknown capability resolves to ``None`` so the Action
    Planner rejects it (the Brain can only act through registered capabilities).
    """

    def __init__(self) -> None:
        self._caps: dict = {}

    def register(self, cap: Capability) -> None:
        if isinstance(cap, Capability) and cap.name:
            self._caps[cap.name] = cap

    def get(self, name: str) -> Optional[Capability]:
        return self._caps.get(str(name or ""))

    def names(self) -> list:
        return sorted(self._caps.keys())

    def all(self) -> list:
        return [self._caps[n] for n in self.names()]

    def resolve_provider(
        self,
        cap: Capability,
        *,
        preferred: str = "",
        available: Optional[set] = None,
    ) -> Optional[ProviderBinding]:
        """Choose the provider for a capability (Article 11). Deterministic.

        Preference order: an explicit ``preferred`` provider (if it is a
        candidate and available) → the first candidate that is available → when
        no availability set is supplied, the capability's first candidate.
        Returns ``None`` only when an availability filter excludes every
        candidate.
        """
        try:
            candidates = list(cap.providers or [])
            if not candidates:
                return None
            avail = None if available is None else {str(a).lower() for a in available}
            pref = str(preferred or "").strip().lower()
            if pref:
                for b in candidates:
                    if b.provider.lower() == pref and (avail is None or b.provider.lower() in avail):
                        return b
            for b in candidates:
                if avail is None or b.provider.lower() in avail:
                    return b
            return None
        except Exception as exc:  # noqa: BLE001 — selection must never raise
            logger.debug("[capabilities] resolve_provider(%s) fault: %s",
                         getattr(cap, "name", "?"), exc)
            return None

    def to_dict(self) -> dict:
        return {"capabilities": [c.to_dict() for c in self.all()]}


# Semantic capability names — the vocabulary the Brain authors objectives in.
CAP_NOOP = "noop"
CAP_OPERATOR_NOTIFY = "operator.notify"
CAP_GITHUB_CREATE_ISSUE = "github.create_issue"
CAP_DOCS_UPDATE = "docs.update"
CAP_REPORT_PUBLISH = "report.publish"
CAP_RESEARCH_RECORD = "research.record"
CAP_KNOWLEDGE_RETRIEVE = "knowledge.retrieve"


def default_registry() -> CapabilityRegistry:
    """Build the curated default capability allow-list (Part IX Articles 8–10).

    Provider→action strings follow Composio's action naming; the exact strings
    are confirmed against the live catalogue when a key is configured (the
    Composio adapter is the single place they are used).
    """
    reg = CapabilityRegistry()
    reg.register(Capability(
        name=CAP_NOOP, category=CapabilityCategory.NOOP,
        risk_tier=RiskTier.NEGLIGIBLE, providers=(ProviderBinding("none", "noop"),),
        description="Deliberate do-nothing — a first-class operational choice.",
    ))
    reg.register(Capability(
        name=CAP_OPERATOR_NOTIFY, category=CapabilityCategory.NOTIFICATION,
        risk_tier=RiskTier.NEGLIGIBLE,
        providers=(ProviderBinding("slack", "SLACK_SEND_MESSAGE"),
                   ProviderBinding("telegram", "TELEGRAM_SEND_MESSAGE")),
        required_params=("message",),
        description="Notify the operator (Article 10 — operational coordination).",
    ))
    reg.register(Capability(
        name=CAP_GITHUB_CREATE_ISSUE, category=CapabilityCategory.OPERATIONAL,
        risk_tier=RiskTier.MEDIUM,
        providers=(ProviderBinding("github", "GITHUB_CREATE_AN_ISSUE"),),
        required_params=("title", "body"), reversible=True,
        required_permissions=("github:issues:write",),
        description="File an engineering issue (Article 9 — GitHub = engineering memory).",
    ))
    reg.register(Capability(
        name=CAP_DOCS_UPDATE, category=CapabilityCategory.OPERATIONAL,
        risk_tier=RiskTier.MEDIUM,
        providers=(ProviderBinding("notion", "NOTION_ADD_PAGE_CONTENT"),
                   ProviderBinding("googledrive", "GOOGLEDRIVE_CREATE_FILE")),
        required_params=("title", "content"),
        required_permissions=("docs:write",),
        description="Create/update institutional documentation (Article 9/10).",
    ))
    reg.register(Capability(
        name=CAP_REPORT_PUBLISH, category=CapabilityCategory.OPERATIONAL,
        risk_tier=RiskTier.LOW,
        providers=(ProviderBinding("slack", "SLACK_SEND_MESSAGE"),
                   ProviderBinding("notion", "NOTION_ADD_PAGE_CONTENT")),
        required_params=("summary",),
        description="Publish a campaign/operations report (Article 9).",
    ))
    reg.register(Capability(
        name=CAP_RESEARCH_RECORD, category=CapabilityCategory.OPERATIONAL,
        risk_tier=RiskTier.LOW,
        providers=(ProviderBinding("notion", "NOTION_ADD_PAGE_CONTENT"),
                   ProviderBinding("googledrive", "GOOGLEDRIVE_CREATE_FILE")),
        required_params=("title", "content"),
        description="Archive research/learning (Article 10 — research storage).",
    ))
    reg.register(Capability(
        name=CAP_KNOWLEDGE_RETRIEVE, category=CapabilityCategory.KNOWLEDGE_RETRIEVAL,
        risk_tier=RiskTier.NEGLIGIBLE,
        providers=(ProviderBinding("composio_search", "COMPOSIO_SEARCH_SEARCH"),),
        required_params=("query",), reversible=True,
        description="Retrieve external knowledge → becomes Evidence (Article 4/8).",
    ))
    return reg


__all__ = [
    "CapabilityCategory",
    "ProviderBinding",
    "Capability",
    "CapabilityRegistry",
    "default_registry",
    "CAP_NOOP",
    "CAP_OPERATOR_NOTIFY",
    "CAP_GITHUB_CREATE_ISSUE",
    "CAP_DOCS_UPDATE",
    "CAP_REPORT_PUBLISH",
    "CAP_RESEARCH_RECORD",
    "CAP_KNOWLEDGE_RETRIEVE",
]
