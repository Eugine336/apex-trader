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
]
