"""Concept modules — full readings of trading concepts for the native Brain."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
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
from cognition.concepts.strategy_method_readings import StrategyMethodReadings
from cognition.concepts.wyckoff_volume_readings import WyckoffVolumeReadings
from cognition.concepts.instrument_economics_readings import InstrumentEconomicsReadings
from cognition.concepts.execution_cost_readings import ExecutionCostReadings
from cognition.concepts.order_book_readings import OrderBookReadings
from cognition.concepts.tick_microstructure_readings import TickMicrostructureReadings
from cognition.concepts.session_calendar import SessionCalendar
from cognition.concepts.opening_range import OpeningRange
from cognition.concepts.vwap import VWAPConcept
from cognition.concepts.volume_profile import VolumeProfileConcept
from cognition.concepts.seasonality import SeasonalityConcept
from cognition.concepts.range_rotation import RangeRotation
from cognition.concepts.quantitative_state_readings import QuantitativeStateReadings
from cognition.concepts.quantitative_outcome_readings import QuantitativeOutcomeReadings
from cognition.concepts.portfolio_risk_readings import PortfolioRiskReadings
from cognition.concepts.strategy_lens_readings import StrategyLensReadings

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
    SessionCalendar(),
    OpeningRange(),
    VWAPConcept(),
    VolumeProfileConcept(),
    SeasonalityConcept(),
    RangeRotation(),
    RegimeSwitchingConcepts(),
    StrategyMethodReadings(),
    WyckoffVolumeReadings(),
    InstrumentEconomicsReadings(),
    ExecutionCostReadings(),
    OrderBookReadings(),
    TickMicrostructureReadings(),
    QuantitativeStateReadings(),
    QuantitativeOutcomeReadings(),
    PortfolioRiskReadings(),
    StrategyLensReadings(),
)


# One latest completed-bar result per module/symbol. This covers a 67-instrument
# universe without retaining obsolete fingerprints after every new candle.
_READING_CACHE: OrderedDict[
    tuple[str, str, int], tuple[str, tuple[ConceptReading, ...], object]
] = OrderedDict()
_READING_CACHE_LOCK = threading.RLock()
_READING_CACHE_MAX_ENTRIES = 4096


def _bars_fingerprint(symbol: str, bars_by_tf: dict[str, list[dict]]) -> str:
    """Fingerprint every completed input field so historical corrections invalidate cache."""
    digest = hashlib.blake2b(digest_size=20)
    digest.update(str(symbol or "").strip().upper().encode("utf-8"))
    for timeframe in sorted(bars_by_tf):
        digest.update(str(timeframe).upper().encode("utf-8"))
        rows = bars_by_tf[timeframe]
        digest.update(str(len(rows)).encode("ascii"))
        for row in rows:
            if not isinstance(row, dict):
                digest.update(repr(row).encode("utf-8", errors="replace"))
                continue
            for key, value in sorted(row.items(), key=lambda item: str(item[0])):
                digest.update(str(key).encode("utf-8", errors="replace"))
                digest.update(repr(value).encode("utf-8", errors="replace"))
    return digest.hexdigest()


def clear_concept_reading_cache() -> None:
    """Clear completed-bar readings, primarily for deterministic tests/reloads."""
    with _READING_CACHE_LOCK:
        _READING_CACHE.clear()


def read_concept_readings(
    symbol: str,
    bars_by_tf: dict[str, list[dict]],
    context: dict[str, Any] | None = None,
    *,
    modules: tuple[ConceptModule, ...] | None = None,
) -> tuple[list[ConceptReading], list[dict[str, str]]]:
    """Run concept readers with completed-bar caching and per-module timings.

    Pure OHLC readers (the read contract) are cached by a digest of the full
    completed input. Contextual readers always run because tick, depth, portfolio,
    execution-cost and other non-candle inputs can change between bar closes.
    """
    started = time.perf_counter()
    readings: list[ConceptReading] = []
    faults: list[dict[str, str]] = []
    reader_context = context if isinstance(context, dict) else {}
    registered = tuple(modules if modules is not None else CONCEPT_MODULES)
    has_pure_readers = any(
        not callable(getattr(module, "read_with_context", None))
        and not getattr(module, "consumes_concept_readings", False)
        for module in registered
    )
    fingerprint = _bars_fingerprint(symbol, bars_by_tf) if has_pure_readers else ""
    module_metrics: dict[str, dict[str, Any]] = {}
    cache_hits = cache_misses = 0

    for module in registered:
        module_key = str(getattr(module, "key", "unknown") or "unknown")
        contextual_reader = getattr(module, "read_with_context", None)
        cacheable = (
            not callable(contextual_reader)
            and not getattr(module, "consumes_concept_readings", False)
        )
        cache_key = (module_key, str(symbol or "").strip().upper(), id(module))
        module_started = time.perf_counter()
        try:
            produced = None
            cache_hit = False
            if cacheable:
                with _READING_CACHE_LOCK:
                    cached = _READING_CACHE.get(cache_key)
                    if (
                        cached is not None
                        and cached[0] == fingerprint
                        and cached[2] is module
                    ):
                        produced = cached[1]
                        _READING_CACHE.move_to_end(cache_key)
                        cache_hit = True
                        cache_hits += 1
                    else:
                        cache_misses += 1

            if produced is None:
                module_context = reader_context
                if getattr(module, "consumes_concept_readings", False):
                    module_context = {
                        **reader_context,
                        "concept_readings": tuple(readings),
                    }
                produced = (
                    contextual_reader(symbol, bars_by_tf, module_context)
                    if callable(contextual_reader)
                    else module.read(symbol, bars_by_tf)
                )
                if produced is None:
                    raise TypeError(f"{module_key} returned None instead of a reading sequence")
                produced = tuple(produced)
                for reading in produced:
                    if not isinstance(reading, ConceptReading):
                        raise TypeError(
                            f"{module_key} returned {type(reading).__name__}, expected ConceptReading"
                        )
                if cacheable:
                    with _READING_CACHE_LOCK:
                        _READING_CACHE[cache_key] = (fingerprint, produced, module)
                        _READING_CACHE.move_to_end(cache_key)
                        while len(_READING_CACHE) > _READING_CACHE_MAX_ENTRIES:
                            _READING_CACHE.popitem(last=False)
            readings.extend(produced)
            module_metrics[module_key] = {
                "elapsed_ms": round((time.perf_counter() - module_started) * 1000.0, 3),
                "cacheable": cacheable,
                "cache_hit": cache_hit,
                "readings": len(produced),
                "status": "ok",
            }
        except Exception as exc:  # noqa: BLE001 — preserve per-module availability
            logger.warning(
                "[concepts] %s failed for %s: %s: %s",
                module_key, symbol, type(exc).__name__, exc,
            )
            faults.append({
                "module": module_key,
                "exception_type": type(exc).__name__,
                "message": str(exc)[:500],
            })
            module_metrics[module_key] = {
                "elapsed_ms": round((time.perf_counter() - module_started) * 1000.0, 3),
                "cacheable": cacheable,
                "cache_hit": False,
                "readings": 0,
                "status": "failed",
            }

    metrics = {
        "symbol": str(symbol or "").strip().upper(),
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "module_count": len(registered),
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "completed_bars_by_timeframe": {
            str(tf): len(rows) for tf, rows in bars_by_tf.items()
        },
        "modules": module_metrics,
    }
    if isinstance(context, dict):
        context["pipeline_metrics"] = metrics
    return readings, faults


def run_concepts(
    symbol: str,
    bars_by_tf: dict[str, list[dict]],
    context: dict[str, Any] | None = None,
) -> list[Any]:
    """Run every concept module over completed bars and return Evidence.

    A module that faults is logged and represented by a non-directional runtime
    diagnostic. Subsequent modules still run; no market reading is fabricated.
    """
    readings, faults = read_concept_readings(symbol, bars_by_tf, context)
    evidence = [reading_to_evidence(symbol, reading) for reading in readings]
    for fault in faults:
        module_key = fault["module"]
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
                "exception_type": fault["exception_type"],
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
    "percentile_rank", "reading_to_evidence", "read_concept_readings", "run_concepts",
    "clear_concept_reading_cache",
]
