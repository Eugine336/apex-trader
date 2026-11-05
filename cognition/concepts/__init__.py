"""Concept modules — full readings of trading concepts for the native Brain."""

from __future__ import annotations

import logging
from typing import Any

from cognition.concepts.base import (
    FLAT, LONG, SHORT, ConceptModule, ConceptReading, Implication,
    percentile_rank, reading_to_evidence,
)
from cognition.contracts import Evidence, EvidenceDomain
from cognition.concepts.crt import CandleRangeTheory
from cognition.concepts.liquidity import LiquidityPools
from cognition.concepts.market_structure import MarketStructure
from cognition.concepts.price_delivery import PriceDeliveryConcepts
from cognition.concepts.price_action import PriceActionConcepts
from cognition.concepts.swing_geometry import SwingGeometryConcepts
from cognition.concepts.structure_patterns import StructurePatternConcepts
from cognition.concepts.indicator_context import IndicatorContextConcepts
from cognition.concepts.pivot_points import PivotPointConcepts
from cognition.concepts.power_of_three import PowerOfThreeConcepts
from cognition.concepts.regime_switching import RegimeSwitchingConcepts

logger = logging.getLogger("apex.cognition.concepts")

# Every concept module registers here. Order is not priority: the Brain weighs readings.
CONCEPT_MODULES: tuple[ConceptModule, ...] = (
    CandleRangeTheory(),
    MarketStructure(),
    LiquidityPools(),
    PriceDeliveryConcepts(),
    PriceActionConcepts(),
    SwingGeometryConcepts(),
    StructurePatternConcepts(),
    IndicatorContextConcepts(),
    PivotPointConcepts(),
    PowerOfThreeConcepts(),
    RegimeSwitchingConcepts(),
)


def run_concepts(symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[Any]:
    """Run every concept module over completed bars and return Evidence.

    A module that faults is logged at WARNING and skipped; it never breaks the
    cycle, and it never fabricates a reading.
    """
    evidence: list[Any] = []
    for module in CONCEPT_MODULES:
        try:
            for reading in module.read(symbol, bars_by_tf):
                evidence.append(reading_to_evidence(symbol, reading))
        except Exception as exc:  # noqa: BLE001
            module_key = str(getattr(module, "key", "unknown") or "unknown")
            logger.warning("[concepts] %s failed for %s: %s: %s",
                           module_key, symbol, type(exc).__name__, exc)
            # A failed reader is a data-availability failure, not evidence that
            # its concept is absent. Preserve the failure in the same evidence
            # stream so the Brain can distinguish unavailable analysis from a
            # genuine negative/neutral market observation. This diagnostic is
            # explicitly non-directional and carries no verified market time.
            evidence.append(Evidence(
                source_module=f"concept_runtime.{module_key}",
                domain=EvidenceDomain.OTHER,
                symbol=str(symbol or ""),
                observation="A registered concept reader failed; its market interpretation is unavailable.",
                confidence=0.0,
                uncertainty=1.0,
                polarity=0.0,
                measurements={
                    "source_kind": "concept_runtime_diagnostic",
                    "concept_module": module_key,
                    "status": "reader_failed",
                    "exception_type": type(exc).__name__,
                    "reading_unavailable": True,
                    "non_market_evidence": True,
                },
                relevance_horizon_seconds=300.0,
                data_basis="runtime_health_diagnostic",
                provisional=True,
                independence_group=f"concept_runtime_health:{module_key}",
            ))
    return evidence


__all__ = [
    "CONCEPT_MODULES", "ConceptModule", "ConceptReading", "Implication", "FLAT", "LONG", "SHORT",
    "percentile_rank", "reading_to_evidence", "run_concepts",
]
