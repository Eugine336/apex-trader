"""Concept modules — full readings of trading concepts for the native Brain."""

from __future__ import annotations

import logging
from typing import Any

from cognition.concepts.base import (
    FLAT, LONG, SHORT, ConceptModule, ConceptReading, Implication,
    percentile_rank, reading_to_evidence,
)
from cognition.concepts.crt import CandleRangeTheory
from cognition.concepts.liquidity import LiquidityPools
from cognition.concepts.market_structure import MarketStructure
from cognition.concepts.price_delivery import PriceDeliveryConcepts
from cognition.concepts.price_action import PriceActionConcepts
from cognition.concepts.swing_geometry import SwingGeometryConcepts
from cognition.concepts.structure_patterns import StructurePatternConcepts

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
            logger.warning("[concepts] %s failed for %s: %s: %s",
                           getattr(module, "key", "?"), symbol, type(exc).__name__, exc)
    return evidence


__all__ = [
    "CONCEPT_MODULES", "ConceptModule", "ConceptReading", "Implication", "FLAT", "LONG", "SHORT",
    "percentile_rank", "reading_to_evidence", "run_concepts",
]
