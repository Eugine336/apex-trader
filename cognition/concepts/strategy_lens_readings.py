"""Strategy-lens compositions built from full method readings.

A lens is a style-specific view over method evidence, not a separate measurement
or strategy engine. It preserves the method lineage, missing inputs, structural
geometry and known failure modes for the Brain to challenge.
"""
from __future__ import annotations

from typing import Any

from cognition.concepts.auction_utils import clean_bars, last_source_epoch
from cognition.concepts.base import TIMEFRAME_SECONDS, ConceptReading, finite
from cognition.contracts import EvidenceDomain
from cognition.strategy_knowledge import STRATEGY_LENSES


LENS_METHODS: dict[str, tuple[str, ...]] = {
    "ict_smc": ("ict_smc", "order_blocks", "market_structure", "inducement", "displacement"),
    "trend_following": ("trend_following", "roc_momentum", "momentum_breakout"),
    "momentum": ("momentum_breakout", "roc_momentum", "displacement"),
    "breakout": ("momentum_breakout", "displacement"),
    "failed_breakout": ("failed_auction", "liquidity_sweep", "inducement", "break_retest"),
    "mean_reversion": ("zscore_mean", "mean_reversion"),
    "range_auction": ("range_rotation", "vwap"),
    "candle_price_action": ("candlestick_patterns", "failed_auction", "break_retest"),
    "order_flow_auction": ("wyckoff_effort_result", "volume_profile", "volume_indicators", "order_book"),
    "volatility_regime": ("realized_volatility", "bollinger", "momentum_breakout"),
    "wyckoff": ("wyckoff", "volume_indicators", "power_of_three"),
    "session_intraday": ("opening_range", "vwap", "power_of_three", "range_rotation"),
    "event_macro": ("macro_rates", "macro_growth", "event_driven"),
    "cross_asset": ("cross_asset", "currency_strength"),
    "statistical_quant": ("zscore_mean", "time_series", "bayesian"),
    "microstructure": ("tick_microstructure", "execution_cost"),
    "support_resistance": ("support_resistance", "market_structure", "pivot_points", "crt"),
    "moving_average": ("moving_averages",),
    "oscillator_divergence": ("divergence", "stochastic", "rsi", "macd"),
    "trend_strength": ("adx_directional", "trend_following"),
    "fibonacci_geometry": ("fibonacci", "dealing_range", "ote"),
    "gap_opening_range": ("opening_range", "gap_analysis", "session_calendar"),
    "volatility_breakout": ("realized_volatility", "bollinger", "keltner_donchian"),
    "seasonality_cycles": ("seasonality",),
    "relative_value": ("relative_value", "cross_asset"),
    "carry_funding": ("carry",),
    "news_event_reaction": ("macro_growth", "event_driven"),
    "correlation_exposure": ("portfolio_risk",),
    "execution_costs": ("execution_cost", "market_impact"),
    "risk_of_ruin": ("portfolio_risk", "monte_carlo"),
    "auction_market_profile": ("volume_profile", "range_rotation"),
    "wyckoff_effort_result": ("wyckoff_effort_result", "wyckoff"),
    "pattern_discovery": ("anomaly_detection", "clustering"),
    "execution_order_types": ("execution_cost", "market_impact"),
    "learning_calibration": ("calibration",),
    "derivatives_context": ("derivatives", "instrument_economics"),
}

# These lenses have a real composition reader, but their complete interpretation
# depends on named feeds or point-in-time records not supplied by OHLC alone.
DATA_GATED_LENSES: dict[str, str] = {
    "range_auction": "verified session boundaries and source-verified traded-volume provenance for VWAP",
    "order_flow_auction": "trade-level volume-at-price and broker DOM/depth where available",
    "session_intraday": "authoritative venue session calendar and verified session VWAP inputs",
    "event_macro": "timestamped economic calendar with actual/forecast/previous and macro series",
    "cross_asset": "synchronized multi-symbol bars with verified common-cutoff timestamps",
    "statistical_quant": "point-in-time per-concept outcome labels for Bayesian posterior updates",
    "microstructure": "source-timestamped bid/ask tick stream and broker DOM/depth where exposed",
    "gap_opening_range": "authoritative venue session calendar with per-bar session identifiers",
    "relative_value": "synchronized multi-symbol histories and stable relationship samples",
    "carry_funding": "verified direction-specific broker swap, funding and rollover data",
    "news_event_reaction": "trusted timestamped event calendar and actual/forecast/previous values",
    "correlation_exposure": "broker-truth whole-book positions and synchronized instrument returns",
    "execution_costs": "source-backed market-impact and fill-history observations",
    "risk_of_ruin": "broker-truth portfolio exposure and validated trade-outcome distributions",
    "auction_market_profile": "trade-level volume-at-price for exact auction nodes",
    "execution_order_types": "current broker constraints, quote/depth and empirical fill probabilities",
    "learning_calibration": "timestamped decisions, outcomes, costs and holdout attribution",
    "derivatives_context": "verified derivatives feed and instrument-specific expiry/roll specifications",
}

# Two lenses are already read directly by their specialist modules.
_EXISTING_LENSES = {"crt", "market_regime_transition"}
FULL_LENSES = tuple(
    key for key in LENS_METHODS
    if key not in DATA_GATED_LENSES and key not in _EXISTING_LENSES
)
if set(LENS_METHODS) | _EXISTING_LENSES != {lens.key for lens in STRATEGY_LENSES}:
    raise RuntimeError("Strategy lens composition map must cover the complete strategy knowledge catalog")
if set(DATA_GATED_LENSES) & set(FULL_LENSES):
    raise RuntimeError("A strategy lens cannot be both FULL and DATA-GATED")


def _verified_bar_epoch(reading: ConceptReading) -> float | None:
    """Return only the verified completed-bar epoch used for replay ordering.

    Provider observation time is a separate clock. It must never be compared
    directly with a candle epoch or silently substituted for the bar timestamp.
    """
    if reading.source_timestamp_verified is not True:
        return None
    epoch = finite(reading.source_bar_epoch)
    return epoch if epoch is not None and epoch > 0.0 else None


def _compact_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Retain compact, decision-relevant method facts without copying raw histories."""
    selected: dict[str, Any] = {}
    tokens = (
        "state", "regime", "invalidation", "objective", "milestone", "anchor",
        "percentile", "breakout", "range", "trend", "zscore", "distance",
        "sample_count", "volatility", "swing", "reversion", "acceptance",
        "strength", "divergence", "value_area",
    )
    for key, value in facts.items():
        if not any(token in str(key).lower() for token in tokens):
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            selected[str(key)] = value
        elif isinstance(value, (list, tuple)) and len(value) <= 6:
            selected[str(key)] = list(value)
        elif isinstance(value, dict) and len(value) <= 8:
            selected[str(key)] = {
                str(k): v for k, v in value.items()
                if v is None or isinstance(v, (str, int, float, bool))
            }
    return selected


def _method_geometry(reading: ConceptReading) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {
        "invalidation_candidates": [],
        "objective_candidates": [],
        "milestone_candidates": [],
        "entry_zone_candidates": [],
        "trail_anchors": [],
        "management_candidates": [],
    }
    for implication in reading.implications:
        result["management_candidates"].append({
            "method": reading.concept,
            "timeframe": reading.timeframe,
            "direction": implication.direction,
            "activated": implication.activated,
            "strength": implication.strength,
            "management": (
                dict(implication.management)
                if isinstance(implication.management, dict) else {}
            ),
            "requires": list(implication.requires),
            "conflicts_with": list(implication.conflicts_with),
        })
        if implication.invalidation_price is not None:
            result["invalidation_candidates"].append({
                "method": reading.concept,
                "timeframe": reading.timeframe,
                "price": implication.invalidation_price,
                "reason": implication.invalidation_reason,
                "direction": implication.direction,
            })
        if implication.objective_price is not None:
            result["objective_candidates"].append({
                "method": reading.concept,
                "timeframe": reading.timeframe,
                "price": implication.objective_price,
                "direction": implication.direction,
            })
        for level in implication.milestones:
            result["milestone_candidates"].append({
                "method": reading.concept, "timeframe": reading.timeframe,
                "price": level, "direction": implication.direction,
            })
    facts = reading.facts if isinstance(reading.facts, dict) else {}
    invalidation = finite(facts.get("structural_invalidation_level"))
    if invalidation is None:
        invalidation = finite(facts.get("invalidation_price"))
    if invalidation is not None:
        result["invalidation_candidates"].append({
            "method": reading.concept, "timeframe": reading.timeframe,
            "price": invalidation, "reason": str(facts.get("invalidation_reason", "") or ""),
            "direction": None,
        })
    objective = finite(facts.get("next_objective_from_confirmed_structure"))
    if objective is None:
        objective = finite(facts.get("objective_price"))
    if objective is not None:
        result["objective_candidates"].append({
            "method": reading.concept, "timeframe": reading.timeframe,
            "price": objective, "direction": None,
        })
    anchors = facts.get("trail_anchor_levels")
    if isinstance(anchors, (list, tuple)):
        for level in anchors:
            price = finite(level)
            if price is not None:
                result["trail_anchors"].append({
                    "method": reading.concept, "timeframe": reading.timeframe, "price": price,
                })
    for key in ("entry_zone", "entry_zones", "preferred_entry_zone"):
        zone = facts.get(key)
        if isinstance(zone, dict):
            result["entry_zone_candidates"].append({
                "method": reading.concept, "timeframe": reading.timeframe, "zone": dict(zone),
            })
        elif isinstance(zone, (list, tuple)) and len(zone) <= 4:
            result["entry_zone_candidates"].append({
                "method": reading.concept, "timeframe": reading.timeframe, "zone": list(zone),
            })
    return result


class StrategyLensReadings:
    """Compose strategy-style readings from already-produced method readings."""

    key = "strategy_lens_readings"
    family = "strategy_lens_composition"
    covers_methods: tuple[str, ...] = ()
    covers_lenses = FULL_LENSES
    data_gated_lenses = DATA_GATED_LENSES
    consumes_concept_readings = True

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        return []

    def read_with_context(
        self, symbol: str, bars_by_tf: dict[str, list[dict]], context: dict[str, Any],
    ) -> list[ConceptReading]:
        prior = context.get("concept_readings", ())
        method_readings = [
            item for item in prior
            if isinstance(item, ConceptReading) and item.concept in {
                method for methods in LENS_METHODS.values() for method in methods
            }
        ]
        cleaned_by_timeframe = {
            timeframe: clean_bars(raw)
            for timeframe, raw in (bars_by_tf or {}).items()
            if timeframe in TIMEFRAME_SECONDS
        }
        timeframes = [
            timeframe for timeframe, bars in cleaned_by_timeframe.items() if bars
        ]
        catalog = {lens.key: lens for lens in STRATEGY_LENSES}
        results: list[ConceptReading] = []

        for timeframe in timeframes:
            bars = cleaned_by_timeframe[timeframe]
            cutoff_epoch = last_source_epoch(bars)
            for lens_key, required_methods in LENS_METHODS.items():
                candidates = [
                    item for item in method_readings
                    if item.timeframe == timeframe and item.concept in required_methods
                ]
                # Historical composition is bounded by the latest verified
                # completed bar supplied for this timeframe. Unverified or
                # future-dated method readings cannot enter the replay snapshot.
                eligible_candidates = [
                    item for item in candidates
                    if cutoff_epoch is not None
                    and (bar_epoch := _verified_bar_epoch(item)) is not None
                    and bar_epoch <= cutoff_epoch
                ]
                eligible_concepts = {item.concept for item in eligible_candidates}
                unverified_methods = sorted({
                    item.concept for item in candidates
                    if _verified_bar_epoch(item) is None
                    and item.concept not in eligible_concepts
                })
                future_methods = sorted({
                    item.concept for item in candidates
                    if cutoff_epoch is not None
                    and (bar_epoch := _verified_bar_epoch(item)) is not None
                    and bar_epoch > cutoff_epoch
                    and item.concept not in eligible_concepts
                })
                selected: dict[str, ConceptReading] = {}
                for method in required_methods:
                    rows = [item for item in eligible_candidates if item.concept == method]
                    if rows:
                        selected[method] = max(
                            rows, key=lambda item: _verified_bar_epoch(item) or 0.0,
                        )
                missing = [method for method in required_methods if method not in selected]
                applicable = [item for item in selected.values() if item.applicable]
                inapplicable = [item.concept for item in selected.values() if not item.applicable]
                all_methods_available = not missing
                all_methods_applicable = all_methods_available and len(applicable) == len(required_methods)
                unknowns: list[str] = []
                if cutoff_epoch is None:
                    unknowns.append(
                        "no source-verified completed-bar cutoff is available; historical lens composition is not evaluable"
                    )
                if unverified_methods:
                    unknowns.append(
                        "method readings excluded because source timestamps are unverified: "
                        + ", ".join(unverified_methods)
                    )
                if future_methods:
                    unknowns.append(
                        "future-dated method readings excluded beyond the completed-bar replay cutoff: "
                        + ", ".join(future_methods)
                    )
                if missing:
                    unknowns.append(
                        "underlying method readings unavailable: " + ", ".join(missing)
                    )
                for method, item in selected.items():
                    unknowns.extend(f"{method}: {reason}" for reason in item.unknowns)
                data_gate = DATA_GATED_LENSES.get(lens_key)
                if data_gate:
                    unknowns.append("complete lens interpretation requires " + data_gate)
                if inapplicable:
                    unknowns.append(
                        "required methods currently inapplicable: " + ", ".join(sorted(inapplicable))
                    )
                if not applicable:
                    unknowns.append("no applicable underlying method reading is currently available")

                summaries = {}
                geometry = {
                    "invalidation_candidates": [],
                    "objective_candidates": [],
                    "milestone_candidates": [],
                    "entry_zone_candidates": [],
                    "trail_anchors": [],
                    "management_candidates": [],
                }
                for method, item in selected.items():
                    summaries[method] = {
                        "applicable": item.applicable,
                        "applicability_reason": item.applicability_reason,
                        "observation": item.observation,
                        "source_bar_epoch": item.source_bar_epoch,
                        "source_timestamp_epoch": item.source_timestamp_epoch,
                        "source_timestamp_verified": item.source_timestamp_verified is True,
                        "independence_group": item.independence_group,
                        "unknowns": list(item.unknowns),
                        "facts": _compact_facts(item.facts if isinstance(item.facts, dict) else {}),
                        "implications": [implication.to_dict() for implication in item.implications],
                    "management": [
                        dict(implication.management)
                        if isinstance(implication.management, dict) else {}
                        for implication in item.implications
                    ],
                    }
                    method_geometry = _method_geometry(item)
                    for key, values in method_geometry.items():
                        geometry[key].extend(values)

                lens = catalog[lens_key]
                # A completed-bar epoch is trusted only when each method reading
                # explicitly verifies its source timestamp. Never promote a merely
                # present timestamp into verified provenance.
                provider_epochs = [
                    (
                        finite(item.source_timestamp_epoch)
                        if item.source_timestamp_verified is True
                        and finite(item.source_timestamp_epoch) is not None
                        and finite(item.source_timestamp_epoch) > 0.0
                        else None
                    )
                    for item in selected.values()
                ]
                provider_epoch_values = {
                    value for value in provider_epochs if value is not None
                }
                source_timestamp_epoch = (
                    next(iter(provider_epoch_values))
                    if selected and len(provider_epoch_values) == 1
                    and all(value is not None for value in provider_epochs)
                    else None
                )
                bar_epochs = [_verified_bar_epoch(item) for item in selected.values()]
                bar_epoch_values = {value for value in bar_epochs if value is not None}
                source_bar_epoch = (
                    next(iter(bar_epoch_values))
                    if selected and all_methods_available
                    and len(selected) == len(required_methods)
                    and len(bar_epoch_values) == 1
                    and all(value is not None for value in bar_epochs)
                    else None
                )
                if selected and source_bar_epoch is None:
                    unknowns.append(
                        "underlying method readings are not all synchronized to one source-verified completed-bar timestamp"
                    )
                current_bar_synchronized = (
                    source_bar_epoch is not None
                    and cutoff_epoch is not None
                    and source_bar_epoch == cutoff_epoch
                )
                if (
                    selected and source_bar_epoch is not None
                    and cutoff_epoch is not None
                    and source_bar_epoch < cutoff_epoch
                ):
                    unknowns.append(
                        "underlying method readings are synchronized to each other but stale relative to the latest completed-bar cutoff"
                    )
                lens_applicable = (
                    all_methods_applicable
                    and data_gate is None
                    and current_bar_synchronized
                )
                style_state = (
                    "complete_method_context" if lens_applicable
                    else "data_gated" if data_gate
                    else "unsynchronized_method_context"
                    if all_methods_applicable and source_bar_epoch is None
                    else "stale_method_context"
                    if all_methods_applicable and source_bar_epoch is not None
                    and cutoff_epoch is not None and source_bar_epoch < cutoff_epoch
                    else "partial_method_context" if selected
                    else "not_evaluable"
                )

                # Do not synthesize independence for a composition. A shared
                # group is valid only when every input explicitly declares the
                # same OHLC group and every input is OHLC-based.
                source_groups = [
                    (
                        str(item.independence_group or "").strip()
                        if (item.data_basis or "completed_ohlc").startswith("completed_ohlc")
                        else ""
                    )
                    for item in selected.values()
                ]
                groups = {group for group in source_groups if group}
                independence_group = (
                    next(iter(groups))
                    if selected and len(groups) == 1 and all(source_groups)
                    else ""
                )
                if len(groups) > 1 or (groups and not all(source_groups)):
                    unknowns.append(
                        "composite lens spans multiple or incomplete source groups; no single independence group is asserted"
                    )
                bases = {
                    item.data_basis or "completed_ohlc" for item in selected.values()
                }
                data_basis = next(iter(bases)) if len(bases) == 1 else "mixed_concept_sources"
                references = {
                    item.reference_close for item in selected.values()
                    if item.reference_close is not None
                }
                reference_close = next(iter(references)) if len(references) == 1 else None
                reference_timeframes = {
                    item.reference_timeframe for item in selected.values()
                    if item.reference_timeframe
                }
                reference_timeframe = next(iter(reference_timeframes)) if len(reference_timeframes) == 1 else ""

                facts = {
                    "evidence_source_module": f"concept.strategy_lens.{lens_key}.{timeframe.lower()}",
                    "lens_key": lens_key,
                    "lens_purpose": lens.purpose,
                    "required_methods": list(required_methods),
                    "available_methods": sorted(selected),
                    "missing_methods": missing,
                    "unverified_method_readings": unverified_methods,
                    "future_dated_method_readings": future_methods,
                    "as_of_source_epoch": cutoff_epoch,
                    "missing_methods_semantics": (
                        "No eligible ConceptReading was available for this method on this symbol/timeframe at the replay cutoff. "
                        "The reader may have emitted no reading, emitted an unverified reading, or emitted a reading beyond the cutoff. "
                        "This does not mean the method implementation is unregistered."
                    ),
                    "applicable_methods": sorted(item.concept for item in applicable),
                    "inapplicable_methods": sorted(inapplicable),
                    "method_summaries": summaries,
                    "method_implications": {
                        method: (
                            [implication.to_dict() for implication in selected[method].implications]
                            if method in selected else []
                        )
                        for method in required_methods
                    },
                    "style_context_state": style_state,
                    "entry_zones": geometry["entry_zone_candidates"],
                    "invalidation_candidates": geometry["invalidation_candidates"],
                    "objective_candidates": geometry["objective_candidates"],
                    "milestone_candidates": geometry["milestone_candidates"],
                    "trail_anchors": geometry["trail_anchors"],
                    "management_candidates": geometry["management_candidates"],
                    "geometry_management_contract": {
                        "stop_basis": "method-supplied invalidation candidates only; the Brain resolves thesis compatibility and risk",
                        "target_basis": "method-supplied objectives and milestones only; no synthetic price targets",
                        "management_basis": "method-supplied management instructions and trail anchors only; no fixed scale-out or trailing constants",
                        "style_key": lens_key,
                        "directional_decision_emitted": False,
                        "execution_authority": False,
                    },
                    "known_failure_modes": [lens.limitation],
                    "data_requirements": list(lens.data_requirements),
                    "data_gated_reason": data_gate,
                    "source_provenance_verified": bool(
                        selected and source_bar_epoch is not None
                    ),
                    "provider_timestamp_alignment": (
                        "synchronized" if source_timestamp_epoch is not None
                        else "not_common_or_not_provided"
                    ),
                    "independence_group_verified": bool(
                        selected and independence_group
                    ),
                    "style_is_not_selected_or_authorized": True,
                    "directional_decision_emitted": False,
                    "entry_authorization_emitted": False,
                }
                results.append(ConceptReading(
                    concept=lens_key,
                    timeframe=timeframe,
                    domain=EvidenceDomain.OTHER,
                    applicable=lens_applicable,
                    applicability_reason=(
                        f"{len(selected)} of {len(required_methods)} required method readings are present; "
                        f"{len(applicable)} are applicable and {len(inapplicable)} are inapplicable; "
                        f"complete style applicability requires every method and all named feeds; "
                        f"style context={style_state}."
                    ),
                    observation=(
                        f"{timeframe} {lens.name} composition: applicable methods="
                        f"{', '.join(sorted(item.concept for item in applicable)) or 'none'}; "
                        f"missing methods={', '.join(missing) or 'none'}. This is context, not strategy authorization."
                    ),
                    implications=(),
                    facts=facts,
                    source_bar_epoch=source_bar_epoch,
                    source_timestamp_epoch=source_timestamp_epoch,
                    source_timestamp_verified=source_bar_epoch is not None,
                    reference_close=reference_close,
                    reference_timeframe=reference_timeframe,
                    unknowns=tuple(dict.fromkeys(unknowns)),
                    data_basis=data_basis,
                    independence_group=independence_group,
                    relevance_horizon_seconds=max(3 * TIMEFRAME_SECONDS[timeframe], 180.0),
                ))
        return results


__all__ = [
    "LENS_METHODS", "DATA_GATED_LENSES", "FULL_LENSES", "StrategyLensReadings",
]
