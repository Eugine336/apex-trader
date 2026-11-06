"""Point-in-time Bayesian, forecast-calibration and bootstrap risk readings.

These methods consume explicit outcome ledgers. Missing or unverified records
remain unknown; the readers never manufacture a prior from current price action,
treat a development sample as holdout, or present a historical bootstrap as a
forecast of the current market.
"""
from __future__ import annotations

import hashlib
import math
import random
from statistics import mean
from typing import Any

from cognition.concepts.auction_utils import clean_bars, last_source_epoch
from cognition.concepts.base import TIMEFRAME_SECONDS, ConceptReading, finite, reference_price
from cognition.contracts import EvidenceDomain


DATA_GATED_METHODS = {
    "bayesian": (
        "source-verified point-in-time concept outcomes labelled by concept and "
        "measured market state, with decision and outcome timestamps"
    ),
    "calibration": (
        "producer-attributed concept probability forecasts with source-verified "
        "outcomes, selection-complete holdout records and forecast timestamps"
    ),
    "monte_carlo": (
        "deduplicated broker-truth closed-trade R outcomes with verified close timestamps; "
        "current-plan stress additionally requires a trusted whole-book plan with an as-of time bounded by the analysis clock"
    ),
}


def _epoch(value: Any) -> float | None:
    parsed = finite(value)
    return parsed if parsed is not None and parsed > 0.0 else None


def _binary_outcome(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    parsed = finite(value)
    if parsed in (0.0, 1.0):
        return int(parsed)
    return None


def _beta_fraction(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz method)."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-300:
        d = 1e-300
    d = 1.0 / d
    result = d
    for m in range(1, 201):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        result *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        delta = d * c
        result *= delta
        if abs(delta - 1.0) < 3e-14:
            break
    return result


def _regularized_beta(x: float, a: float, b: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_fraction(a, b, x) / a
    return 1.0 - front * _beta_fraction(b, a, 1.0 - x) / b


def _beta_quantile(probability: float, alpha: float, beta: float) -> float:
    low, high = 0.0, 1.0
    for _ in range(90):
        middle = (low + high) / 2.0
        if _regularized_beta(middle, alpha, beta) < probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _valid_outcome_rows(
    rows: Any, symbol: str, cutoff: float,
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(rows, (list, tuple)):
        return [], 0
    valid: list[dict[str, Any]] = []
    rejected = 0
    for row in rows:
        if not isinstance(row, dict):
            rejected += 1
            continue
        decision_epoch = _epoch(row.get("decision_epoch"))
        outcome_epoch = _epoch(row.get("outcome_epoch"))
        source_epoch = _epoch(row.get("source_timestamp_epoch"))
        outcome = _binary_outcome(row.get("outcome"))
        row_symbol = str(row.get("symbol", "") or "").strip().upper()
        if (
            row_symbol != symbol
            or not str(row.get("concept", "") or "").strip()
            or row.get("source_timestamp_verified") is not True
            or decision_epoch is None or outcome_epoch is None or source_epoch is None
            or decision_epoch > outcome_epoch or outcome_epoch > cutoff
            or source_epoch < outcome_epoch or source_epoch > cutoff or outcome is None
        ):
            rejected += 1
            continue
        valid.append({
            **row,
            "concept": str(row["concept"]).strip().lower(),
            "outcome": outcome,
            "decision_epoch": decision_epoch,
            "outcome_epoch": outcome_epoch,
            "source_timestamp_epoch": source_epoch,
        })
    return valid, rejected


def _bayesian_reading(
    symbol: str, timeframe: str, bars: list[dict], context: dict[str, Any],
    reference: float | None, reference_tf: str,
) -> ConceptReading:
    cutoff = last_source_epoch(bars)
    unknowns: list[str] = []
    current_state = str(context.get("market_state", "") or "").strip().lower()
    rows, rejected = _valid_outcome_rows(
        context.get("concept_outcome_history"), symbol, cutoff or 0.0,
    )
    by_concept: dict[str, dict[str, Any]] = {}
    for concept in sorted({item["concept"] for item in rows}):
        concept_rows = [item for item in rows if item["concept"] == concept]
        state_rows = [
            item for item in concept_rows
            if current_state and str(item.get("market_state", "") or "").strip().lower() == current_state
        ]
        successes = sum(item["outcome"] for item in state_rows)
        failures = len(state_rows) - successes
        alpha, beta = 0.5 + successes, 0.5 + failures
        if state_rows:
            by_concept[concept] = {
                "market_state": current_state,
                "prior": {"alpha": 0.5, "beta": 0.5, "family": "Jeffreys"},
                "likelihood": {"successes": successes, "failures": failures},
                "posterior": {
                    "alpha": alpha,
                    "beta": beta,
                    "mean": alpha / (alpha + beta),
                    "credible_interval_95": [
                        _beta_quantile(0.025, alpha, beta),
                        _beta_quantile(0.975, alpha, beta),
                    ],
                },
                "sample_count": len(state_rows),
            }
    if cutoff is None:
        unknowns.append("source-verified completed-bar cutoff for point-in-time outcome selection")
    if not current_state:
        unknowns.append("measured current market-state label to condition the posterior")
    if not by_concept:
        unknowns.append("point-in-time concept outcome rows for the current measured market state")
    if rejected:
        unknowns.append(f"{rejected} outcome row(s) rejected for missing, invalid or future provenance")
    latest = max((item["source_timestamp_epoch"] for item in rows), default=None)
    return ConceptReading(
        concept="bayesian", timeframe=timeframe, domain=EvidenceDomain.HISTORICAL_ANALOGUE,
        applicable=bool(by_concept) and cutoff is not None,
        applicability_reason=(
            "Jeffreys-prior Beta-Binomial posteriors are available for source-verified concept outcomes "
            "in the measured current market state."
            if by_concept and cutoff is not None else
            "Current-state Bayesian updating is unavailable without a verified point-in-time outcome ledger."
        ),
        observation=(
            f"{symbol} {timeframe}: Bayesian hit-rate posteriors are reported per concept and current market state; "
            "posterior means are historical evidence, not current opportunity probabilities."
        ),
        implications=(),
        facts={
            "posterior_by_concept": by_concept,
            "current_market_state": current_state or None,
            "outcome_rows_accepted": len(rows),
            "outcome_rows_rejected": rejected,
            "prior_family": "Jeffreys Beta(0.5, 0.5)",
            "posterior_is_current_trade_probability": False,
            "directional_decision_emitted": False,
            "entry_authorization_emitted": False,
            "data_gated_reason": None if by_concept else DATA_GATED_METHODS["bayesian"],
        },
        source_bar_epoch=cutoff,
        source_timestamp_epoch=latest,
        source_timestamp_verified=cutoff is not None,
        reference_close=reference, reference_timeframe=reference_tf,
        unknowns=tuple(unknowns),
        data_basis="point_in_time_concept_outcome_ledger",
        independence_group=f"concept_outcomes:{symbol}",
    )


def _calibration_reading(
    symbol: str, timeframe: str, bars: list[dict], context: dict[str, Any],
    reference: float | None, reference_tf: str,
) -> ConceptReading:
    cutoff = last_source_epoch(bars)
    records = context.get("concept_probability_forecasts")
    accepted: list[dict[str, Any]] = []
    rejected = 0
    if isinstance(records, (list, tuple)):
        for row in records:
            if not isinstance(row, dict):
                rejected += 1
                continue
            probability = finite(row.get("forecast_probability"))
            outcome = _binary_outcome(row.get("outcome"))
            forecast_epoch = _epoch(row.get("forecast_epoch"))
            outcome_epoch = _epoch(row.get("outcome_epoch"))
            source_epoch = _epoch(row.get("source_timestamp_epoch"))
            valid = (
                str(row.get("symbol", "") or "").strip().upper() == symbol
                and bool(str(row.get("concept", "") or "").strip())
                and row.get("source_timestamp_verified") is True
                and row.get("split") == "holdout"
                and row.get("selection_complete") is True
                and probability is not None and 0.0 <= probability <= 1.0
                and outcome is not None
                and forecast_epoch is not None and outcome_epoch is not None
                and source_epoch is not None
                and forecast_epoch < outcome_epoch
                and cutoff is not None and outcome_epoch <= cutoff
                and source_epoch >= outcome_epoch and source_epoch <= cutoff
            )
            if not valid:
                rejected += 1
                continue
            accepted.append({
                "concept": str(row["concept"]).strip().lower(),
                "probability": probability,
                "outcome": outcome,
                "source_timestamp_epoch": source_epoch,
            })
    by_concept: dict[str, dict[str, Any]] = {}
    for concept in sorted({item["concept"] for item in accepted}):
        sample = [item for item in accepted if item["concept"] == concept]
        brier = mean((item["probability"] - item["outcome"]) ** 2 for item in sample)
        forecast_rate = mean(item["probability"] for item in sample)
        observed_rate = mean(item["outcome"] for item in sample)
        by_concept[concept] = {
            "sample_count": len(sample),
            "brier_score": brier,
            "mean_forecast_probability": forecast_rate,
            "observed_frequency": observed_rate,
            "reliability_gap": abs(forecast_rate - observed_rate),
            "holdout_only": True,
            "selection_complete": True,
        }
    unknowns: list[str] = []
    if cutoff is None:
        unknowns.append("source-verified completed-bar cutoff for holdout outcome selection")
    if not by_concept:
        unknowns.append("selection-complete, source-verified concept forecasts with matured holdout outcomes")
    if rejected:
        unknowns.append(f"{rejected} forecast row(s) rejected because holdout, selection or timestamp requirements failed")
    latest = max(
        (item["source_timestamp_epoch"] for item in accepted),
        default=0.0,
    )
    return ConceptReading(
        concept="calibration", timeframe=timeframe, domain=EvidenceDomain.HISTORICAL_ANALOGUE,
        applicable=bool(by_concept) and cutoff is not None,
        applicability_reason=(
            "Per-concept Brier and reliability diagnostics are computed only from selection-complete untouched holdout outcomes."
            if by_concept and cutoff is not None else
            "Forecast calibration is unavailable without mature, selection-complete holdout outcomes."
        ),
        observation=(
            f"{symbol} {timeframe}: per-concept forecast calibration is reported from untouched holdout records; "
            "these diagnostics do not veto a thesis or establish current-regime expectancy."
        ),
        implications=(),
        facts={
            "calibration_by_concept": by_concept,
            "holdout_only": True,
            "selection_complete_only": True,
            "probability_calibration_verified": False,
            "predictive_ability_demonstrated": False,
            "forecast_rows_accepted": len(accepted),
            "forecast_rows_rejected": rejected,
            "directional_decision_emitted": False,
            "entry_authorization_emitted": False,
            "data_gated_reason": None if by_concept else DATA_GATED_METHODS["calibration"],
        },
        source_bar_epoch=cutoff,
        source_timestamp_epoch=latest if latest > 0.0 else None,
        source_timestamp_verified=cutoff is not None,
        reference_close=reference, reference_timeframe=reference_tf,
        unknowns=tuple(unknowns),
        data_basis="selection_complete_holdout_forecast_outcomes",
        independence_group=f"concept_calibration:{symbol}",
    )


def _monte_carlo_reading(
    symbol: str, timeframe: str, bars: list[dict], context: dict[str, Any],
    reference: float | None, reference_tf: str,
) -> ConceptReading:
    cutoff = last_source_epoch(bars)
    records = context.get("trade_outcome_samples")
    plan = context.get("portfolio_plan")
    returns: list[float] = []
    accepted_source_epochs: list[float] = []
    seen: set[str] = set()
    rejected = 0
    portfolio_id = (
        str(plan.get("portfolio_id", "") or "").strip()
        if isinstance(plan, dict) else ""
    )
    if isinstance(records, (list, tuple)):
        for row in records:
            if not isinstance(row, dict):
                rejected += 1
                continue
            trade_id = str(row.get("trade_id", "") or "").strip()
            closed_epoch = _epoch(row.get("closed_epoch"))
            source_epoch = _epoch(row.get("source_timestamp_epoch"))
            value = finite(row.get("return_r"))
            if (
                not trade_id or trade_id in seen
                or not portfolio_id
                or str(row.get("portfolio_id", "") or "").strip() != portfolio_id
                or row.get("source_timestamp_verified") is not True
                or closed_epoch is None or source_epoch is None
                or cutoff is None or closed_epoch > cutoff
                or source_epoch < closed_epoch or source_epoch > cutoff
                or value is None
            ):
                rejected += 1
                continue
            seen.add(trade_id)
            returns.append(value)
            accepted_source_epochs.append(source_epoch)

    equity = finite(plan.get("equity")) if isinstance(plan, dict) else None
    risk_fraction = finite(plan.get("risk_fraction")) if isinstance(plan, dict) else None
    raw_trade_count = finite(plan.get("planned_trade_count")) if isinstance(plan, dict) else None
    plan_epoch = _epoch(plan.get("plan_as_of_epoch")) if isinstance(plan, dict) else None
    analysis_epoch = _epoch(context.get("analysis_epoch"))
    plan_valid = bool(
        isinstance(plan, dict)
        and plan.get("portfolio_scope") == "whole_book"
        and plan.get("plan_attested") is True
        and portfolio_id
        and cutoff is not None and plan_epoch is not None and plan_epoch >= cutoff
        and analysis_epoch is not None and plan_epoch <= analysis_epoch
        and equity is not None and equity > 0.0
        and risk_fraction is not None and 0.0 < risk_fraction <= 1.0
        and raw_trade_count is not None and raw_trade_count.is_integer()
        and raw_trade_count > 0
    )
    planned_trade_count = int(raw_trade_count) if plan_valid and raw_trade_count is not None else 0
    iterations_raw = finite(context.get("monte_carlo_iterations"))
    iterations = int(iterations_raw) if iterations_raw is not None and iterations_raw >= 100 else 5000
    iterations = min(iterations, 50000)
    digest = hashlib.sha256(
        (
            symbol + "|" + portfolio_id + "|"
            + "|".join(f"{value:.12g}" for value in returns)
            + f"|{equity}|{risk_fraction}|{planned_trade_count}"
        ).encode("utf-8")
    ).hexdigest()
    rng = random.Random(int(digest[:16], 16))
    terminal_returns: list[float] = []
    max_drawdowns: list[float] = []
    final_equities: list[float] = []
    portfolio_drawdowns: list[float] = []
    ruin_count = 0
    if len(returns) >= 2 and plan_valid and equity is not None and risk_fraction is not None:
        for _ in range(iterations):
            historical_r = 0.0
            portfolio_equity = equity
            portfolio_peak = equity
            drawdown_r = 0.0
            drawdown_fraction = 0.0
            ruined = False
            historical_peak = 0.0
            for _ in range(planned_trade_count):
                outcome_r = rng.choice(returns)
                historical_r += outcome_r
                historical_peak = max(historical_peak, historical_r)
                drawdown_r = max(drawdown_r, historical_peak - historical_r)
                if not ruined:
                    portfolio_equity *= 1.0 + risk_fraction * outcome_r
                    if portfolio_equity <= 0.0:
                        portfolio_equity = 0.0
                        ruined = True
                    portfolio_peak = max(portfolio_peak, portfolio_equity)
                    if portfolio_peak > 0.0:
                        drawdown_fraction = max(
                            drawdown_fraction,
                            (portfolio_peak - portfolio_equity) / portfolio_peak,
                        )
            terminal_returns.append(historical_r)
            max_drawdowns.append(drawdown_r)
            final_equities.append(portfolio_equity)
            portfolio_drawdowns.append(drawdown_fraction)
            ruin_count += int(ruined)

    tail = _quantile(terminal_returns, 0.05)
    tail_values = [value for value in terminal_returns if tail is not None and value <= tail]
    unknowns: list[str] = []
    if cutoff is None:
        unknowns.append("source-verified completed-bar cutoff for trade outcome selection")
    if len(returns) < 2:
        unknowns.append("at least two distinct broker-truth whole-book trade R outcomes with verified close timestamps")
    if not plan_valid:
        unknowns.append(
            "source-verified whole-book portfolio plan, timestamped after the completed-bar cutoff and no later than analysis time, with equity, risk fraction and planned trade count"
        )
    if rejected:
        unknowns.append(f"{rejected} trade outcome row(s) rejected for duplicate, invalid or future provenance")
    unknowns.append(
        "bootstrap assumes the supplied trade-return sample is representative; dependence, regime drift and execution changes may invalidate it"
    )
    return ConceptReading(
        concept="monte_carlo", timeframe=timeframe, domain=EvidenceDomain.PORTFOLIO,
        applicable=len(returns) >= 2 and plan_valid and cutoff is not None,
        applicability_reason=(
            "A deterministic-seed bootstrap stresses the source-verified whole-book plan against broker-truth closed-trade R outcomes."
            if len(returns) >= 2 and plan_valid and cutoff is not None else
            "Portfolio plan stress is unavailable without verified whole-book outcomes and a current source-verified plan."
        ),
        observation=(
            f"{symbol} {timeframe}: Monte Carlo stress reports historical outcome dispersion, "
            "whole-book plan drawdown and ruin frequency; it does not approve execution."
        ),
        implications=(),
        facts={
            "portfolio_id": portfolio_id or None,
            "portfolio_scope": "whole_book" if plan_valid else "unknown",
            "trade_sample_count": len(returns),
            "simulation_count": len(terminal_returns),
            "planned_trade_count": planned_trade_count,
            "portfolio_plan_epoch": plan_epoch if plan_valid else None,
            "risk_fraction": risk_fraction if plan_valid else None,
            "bootstrap_terminal_return_r_mean": mean(terminal_returns) if terminal_returns else None,
            "bootstrap_terminal_return_r_5pct": tail,
            "bootstrap_terminal_return_r_95pct": _quantile(terminal_returns, 0.95),
            "bootstrap_expected_shortfall_r_5pct": mean(tail_values) if tail_values else None,
            "monte_carlo_drawdown_95pct_r": _quantile(max_drawdowns, 0.95),
            "portfolio_drawdown_95pct_fraction": _quantile(portfolio_drawdowns, 0.95),
            "portfolio_final_equity_5pct": _quantile(final_equities, 0.05),
            "probability_of_ruin": ruin_count / len(final_equities) if final_equities else None,
            "simulation_method": "deterministic_seed_iid_whole_book_trade_return_bootstrap",
            "current_portfolio_plan_stressed": plan_valid,
            "directional_decision_emitted": False,
            "entry_authorization_emitted": False,
            "data_gated_reason": None if len(returns) >= 2 and plan_valid else DATA_GATED_METHODS["monte_carlo"],
        },
        source_bar_epoch=cutoff,
        source_timestamp_epoch=max(accepted_source_epochs) if accepted_source_epochs else None,
        source_timestamp_verified=cutoff is not None,
        reference_close=reference, reference_timeframe=reference_tf,
        unknowns=tuple(unknowns),
        data_basis="broker_truth_whole_book_trade_outcomes_and_current_plan",
        independence_group=f"portfolio_outcomes:{portfolio_id}" if portfolio_id else "",
    )

class QuantitativeOutcomeReadings:
    key = "quantitative_outcome_readings"
    consumes_concept_readings = True
    family = "quantitative"
    covers_methods: tuple[str, ...] = ()
    covers_lenses: tuple[str, ...] = ()
    data_gated_methods = DATA_GATED_METHODS

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        return self.read_with_context(symbol, bars_by_tf, {})

    def read_with_context(
        self, symbol: str, bars_by_tf: dict[str, list[dict]], context: dict[str, Any],
    ) -> list[ConceptReading]:
        reference, reference_tf = reference_price(bars_by_tf)
        readings: list[ConceptReading] = []
        for timeframe, raw in (bars_by_tf or {}).items():
            if timeframe not in TIMEFRAME_SECONDS:
                continue
            bars = clean_bars(raw)
            if not bars:
                continue
            module_context = dict(context or {})
            if not str(module_context.get("market_state", "") or "").strip():
                prior = module_context.get("concept_readings", ())
                state_reading = next(
                    (
                        item for item in reversed(tuple(prior or ()))
                        if isinstance(item, ConceptReading)
                        and item.concept == "time_series"
                        and item.timeframe == timeframe
                    ),
                    None,
                )
                if state_reading is not None:
                    state = str(
                        state_reading.facts.get("structural_regime_from_confirmed_swings", "")
                        or ""
                    ).strip().lower()
                    if state in {"up", "down", "range_or_transition"}:
                        module_context["market_state"] = state
            readings.extend((
                _bayesian_reading(symbol, timeframe, bars, module_context, reference, reference_tf),
                _calibration_reading(symbol, timeframe, bars, module_context, reference, reference_tf),
                _monte_carlo_reading(symbol, timeframe, bars, module_context, reference, reference_tf),
            ))
        return readings


__all__ = ["DATA_GATED_METHODS", "QuantitativeOutcomeReadings"]
