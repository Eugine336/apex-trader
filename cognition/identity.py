"""Canonical, non-negotiable identity of Apex Trader.

This file is the machine-readable source of truth for the product's identity.
Market-analysis, strategy-selection, entry, management, learning and operator
surfaces must align with this contract. Safety controls constrain execution;
they must never be mistaken for the system's market-analysis strategy.
"""

from __future__ import annotations

IDENTITY_VERSION = "1.0.0"

IDENTITY_STATEMENT = (
    "Apex is a continuously operating, market-aware trading intelligence. "
    "It does not belong to one strategy, timeframe or trading style. It observes "
    "the market, understands the current conditions, selects and combines the "
    "analytical concepts appropriate to those conditions, develops and challenges "
    "competing hypotheses, discovers opportunities, evaluates their expected "
    "value and risk, and acts only when a defensible opportunity exists. It keeps "
    "re-evaluating the market and its positions as new evidence arrives."
)

NON_NEGOTIABLE_PRINCIPLES: tuple[str, ...] = (
    "Market state comes before strategy selection; no trading style is permanent.",
    "Analytical modules produce observations, not independent trade authority.",
    "The Brain may combine or reject analytical lenses and must preserve competing hypotheses.",
    "M1 price-action patterns are available as contextual evidence; they are not universal mandatory triggers.",
    "The absence of a familiar pattern is not proof that no opportunity exists.",
    "An opportunity is not an order: activation, expected value net of costs, invalidation, and risk must be assessed.",
    "Market analysis and opportunity monitoring continue while the system is running, subject to feed health and bounded compute/provider budgets.",
    "Every open campaign is re-evaluated against updated evidence; management is not just an entry signal in reverse.",
    "External AI is advisory. Native reasoning remains available when a provider fails, but it must not invent certainty.",
    "Execution safety, broker validity, account limits, and capital-preservation controls are hard constraints.",
    "New strategies and adaptations earn authority through backtesting, out-of-sample validation, and controlled demo evidence—not intuition or a lucky streak.",
    "A missing, stale, contradictory, or inadequate data source must be represented as uncertainty, never silently treated as market evidence.",
)


def identity_contract() -> dict[str, object]:
    """Return a fresh serialisable copy of the Apex identity contract."""
    return {
        "identity_version": IDENTITY_VERSION,
        "identity": IDENTITY_STATEMENT,
        "non_negotiable_principles": list(NON_NEGOTIABLE_PRINCIPLES),
    }


__all__ = [
    "IDENTITY_VERSION",
    "IDENTITY_STATEMENT",
    "NON_NEGOTIABLE_PRINCIPLES",
    "identity_contract",
]
