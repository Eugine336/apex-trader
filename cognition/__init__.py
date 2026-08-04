"""APEX TRADER — Cognition package (the Single Reasoner's core vocabulary).

This package holds the constitutional data contracts (:mod:`cognition.contracts`)
that every subsystem must speak: :class:`~cognition.contracts.Evidence`,
:class:`~cognition.contracts.MarketState`,
:class:`~cognition.contracts.DecisionPackage` and
:class:`~cognition.contracts.CampaignSpecification`.

Per the APEX Constitution (Part I, Article 4; Part II), only the AI Cognitive
Brain reasons; everything else produces Evidence or consumes a DecisionPackage /
CampaignSpecification. These types are inert records — they carry no decision
logic of their own.
"""

from cognition.contracts import (
    REQUIRED_QUESTIONS,
    CampaignSpecification,
    DecisionPackage,
    DecisionType,
    Evidence,
    EvidenceDomain,
    MarketState,
    domain_from,
)

__all__ = [
    "Evidence",
    "EvidenceDomain",
    "domain_from",
    "MarketState",
    "DecisionType",
    "DecisionPackage",
    "CampaignSpecification",
    "REQUIRED_QUESTIONS",
    # Cognitive core (imported lazily below to keep contracts import-light).
    "CognitiveBrain",
    "BrainOutput",
    "EvidenceConsolidator",
    "BrainActionBridge",
    "CognitionLoop",
    "OriginationIntent",
    "translate",
    "CampaignMemoryStore",
    "get_campaign_memory",
]


def __getattr__(name: str):
    # Lazy re-export of the brain/loop so ``import cognition.contracts`` stays
    # dependency-free while ``from cognition import CognitiveBrain`` still works.
    if name in ("CognitiveBrain", "BrainOutput", "PositionView", "LONG", "SHORT", "FLAT"):
        from cognition import brain as _brain
        return getattr(_brain, name)
    if name in ("EvidenceConsolidator", "BrainActionBridge", "CognitionLoop", "SymbolsProvider"):
        from cognition import loop as _loop
        return getattr(_loop, name)
    if name in ("classify_domain", "evidence_from_thesis_status", "evidence_from_votes",
                "evidence_from_analogues"):
        from cognition import evidence_adapters as _ea
        return getattr(_ea, name)
    if name in ("CognitionGate", "GateVerdict", "normalise_mode",
                "MODE_OFF", "MODE_SHADOW", "MODE_VETO", "MODE_AUTHORITATIVE"):
        from cognition import gate as _gate
        return getattr(_gate, name)
    if name in ("ManagementGate", "classify_action"):
        from cognition import management_gate as _mg
        return getattr(_mg, name)
    if name in ("OriginationIntent", "translate"):
        from cognition import campaign_translator as _ct
        return getattr(_ct, name)
    if name in ("CampaignMemoryStore", "fingerprint_from_market_state",
                "fingerprint_similarity", "get_campaign_memory"):
        from cognition import memory as _mem
        return getattr(_mem, name)
    raise AttributeError(f"module 'cognition' has no attribute {name!r}")
