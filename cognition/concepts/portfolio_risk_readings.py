"""Broker-truth portfolio risk evidence. No sizing or execution authority."""
from __future__ import annotations

import math
from statistics import mean
from typing import Any

from cognition.concepts.auction_utils import clean_bars, last_source_epoch
from cognition.concepts.base import TIMEFRAME_SECONDS, ConceptReading, finite, reference_price
from cognition.contracts import EvidenceDomain

DATA_GATED_METHODS = {
    "portfolio_risk": (
        "source-verified complete whole-book broker positions with per-position risk and notional, "
        "account equity, synchronized portfolio returns, equity history and pairwise correlation history"
    ),
}


def _epoch(value: Any) -> float | None:
    parsed = finite(value)
    return parsed if parsed is not None and parsed > 0.0 else None


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _verified_series(rows: Any, value_key: str, snapshot_epoch: float, analysis_epoch: float):
    if not isinstance(rows, (list, tuple)):
        return [], 0
    accepted: list[tuple[float, float]] = []
    seen: set[float] = set()
    rejected = 0
    for row in rows:
        if not isinstance(row, dict):
            rejected += 1
            continue
        stamp = _epoch(row.get("source_timestamp_epoch"))
        value = finite(row.get(value_key))
        if (
            row.get("source_timestamp_verified") is not True
            or stamp is None or stamp in seen
            or stamp > snapshot_epoch or stamp > analysis_epoch
            or value is None
        ):
            rejected += 1
            continue
        seen.add(stamp)
        accepted.append((stamp, value))
    accepted.sort(key=lambda item: item[0])
    return accepted, rejected


def _read_portfolio(
    symbol: str, timeframe: str, bars: list[dict], context: dict[str, Any],
    reference: float | None, reference_tf: str,
) -> ConceptReading:
    cutoff = last_source_epoch(bars)
    snapshot = context.get("portfolio_risk_snapshot")
    analysis_epoch = _epoch(context.get("analysis_epoch"))
    snapshot_epoch = _epoch(snapshot.get("source_timestamp_epoch")) if isinstance(snapshot, dict) else None
    portfolio_id = str(snapshot.get("portfolio_id", "") or "").strip() if isinstance(snapshot, dict) else ""
    equity = finite(snapshot.get("equity")) if isinstance(snapshot, dict) else None
    confirmed = set(snapshot.get("confirmed_platforms", []) or []) if isinstance(snapshot, dict) else set()
    failed = set(snapshot.get("failed_platforms", []) or []) if isinstance(snapshot, dict) else set()
    snapshot_valid = bool(
        isinstance(snapshot, dict) and snapshot.get("complete") is True
        and snapshot.get("source_timestamp_verified") is True
        and snapshot_epoch is not None and analysis_epoch is not None
        and snapshot_epoch <= analysis_epoch and cutoff is not None and snapshot_epoch >= cutoff
        and portfolio_id and equity is not None and equity > 0.0
        and confirmed and not failed
    )
    unknowns: list[str] = []
    if not snapshot_valid:
        unknowns.append(
            "complete broker-confirmed whole-book snapshot with verified source timestamp, positive equity and no failed platforms"
        )

    raw_positions = snapshot.get("positions") if isinstance(snapshot, dict) else None
    positions_valid = snapshot_valid and isinstance(raw_positions, list)
    positions: list[dict[str, Any]] = []
    ids: set[str] = set()
    if positions_valid:
        for row in raw_positions:
            if not isinstance(row, dict):
                positions_valid = False
                break
            position_id = str(row.get("position_id", "") or "").strip()
            row_epoch = _epoch(row.get("source_timestamp_epoch"))
            risk, notional = finite(row.get("risk_account_ccy")), finite(row.get("notional_account_ccy"))
            net, currency = finite(row.get("net_exposure_account_ccy")), row.get("currency_exposure")
            if (
                not position_id or position_id in ids
                or row.get("source_timestamp_verified") is not True
                or row_epoch is None or snapshot_epoch is None or row_epoch != snapshot_epoch
                or risk is None or risk < 0.0 or notional is None or notional < 0.0
                or net is None or not isinstance(currency, dict)
                or any(not str(code).strip() or finite(value) is None for code, value in currency.items())
            ):
                positions_valid = False
                break
            ids.add(position_id)
            positions.append({
                "symbol": str(row.get("symbol", "") or "").strip().upper(),
                "risk": risk, "notional": notional, "net": net,
                "currency": {str(code).upper(): float(value) for code, value in currency.items()},
            })
    if not positions_valid:
        unknowns.append(
            "every open position must have a unique broker identifier, verified timestamp, account-currency risk, notional and currency exposure"
        )

    returns_rows, rejected_returns = _verified_series(
        snapshot.get("portfolio_returns") if isinstance(snapshot, dict) else None,
        "return_fraction", snapshot_epoch or 0.0, analysis_epoch or 0.0,
    )
    equity_rows, rejected_equity = _verified_series(
        snapshot.get("equity_curve") if isinstance(snapshot, dict) else None,
        "equity", snapshot_epoch or 0.0, analysis_epoch or 0.0,
    )
    if len(returns_rows) < 2:
        unknowns.append("at least two unique source-verified whole-portfolio return observations")
    if len(equity_rows) < 2 or any(value <= 0.0 for _, value in equity_rows):
        unknowns.append("at least two positive source-verified portfolio equity observations")
    if rejected_returns:
        unknowns.append(f"{rejected_returns} portfolio return row(s) rejected for duplicate, invalid or future provenance")
    if rejected_equity:
        unknowns.append(f"{rejected_equity} equity row(s) rejected for duplicate, invalid or future provenance")

    pairs: list[dict[str, Any]] = []
    pair_keys: set[tuple[str, str]] = set()
    pair_epochs: set[float] = set()
    pairs_raw = snapshot.get("correlation_pairs") if isinstance(snapshot, dict) else None
    if isinstance(pairs_raw, list) and snapshot_valid:
        for row in pairs_raw:
            if not isinstance(row, dict):
                continue
            left = str(row.get("symbol_a", "") or "").strip().upper()
            right = str(row.get("symbol_b", "") or "").strip().upper()
            stamp = _epoch(row.get("source_timestamp_epoch"))
            correlation = finite(row.get("correlation"))
            key = tuple(sorted((left, right)))
            if (
                not left or not right or left == right or key in pair_keys
                or row.get("source_timestamp_verified") is not True
                or stamp is None or snapshot_epoch is None or stamp < (cutoff or 0.0)
                or stamp > snapshot_epoch
                or correlation is None or not -1.0 <= correlation <= 1.0
            ):
                continue
            pair_keys.add(key)
            pair_epochs.add(stamp)
            pairs.append({"symbol_a": key[0], "symbol_b": key[1], "correlation": correlation})
    symbols = sorted({item["symbol"] for item in positions if item["symbol"]})
    expected_pairs = len(symbols) * (len(symbols) - 1) // 2
    correlations_valid = (
        expected_pairs == 0
        or (len(pair_keys) == expected_pairs and len(pair_epochs) == 1)
    )
    if not correlations_valid:
        unknowns.append(
            "complete pairwise correlations for every distinct open-position symbol, all synchronized to one timestamp at or after the completed-bar cutoff"
        )

    open_risk = sum(item["risk"] for item in positions) if positions_valid else None
    gross = sum(item["notional"] for item in positions) if positions_valid else None
    net = sum(item["net"] for item in positions) if positions_valid else None
    currencies: dict[str, float] = {}
    if positions_valid:
        for position in positions:
            for code, value in position["currency"].items():
                currencies[code] = currencies.get(code, 0.0) + value
    heat = open_risk / equity * 100.0 if open_risk is not None and equity and equity > 0 else None

    returns = [value for _, value in returns_rows]
    tail_count = max(1, math.ceil(len(returns) * 0.05)) if returns else 0
    tail = sorted(returns)[:tail_count] if returns else []
    var_95 = _quantile(returns, 0.05) if len(returns) >= 2 else None
    es_95 = mean(tail) if len(returns) >= 2 and tail else None
    max_drawdown = None
    if len(equity_rows) >= 2 and all(value > 0.0 for _, value in equity_rows):
        peak = equity_rows[0][1]
        max_drawdown = 0.0
        for _, value in equity_rows[1:]:
            peak = max(peak, value)
            max_drawdown = max(max_drawdown, (peak - value) / peak)
    max_corr = max((abs(item["correlation"]) for item in pairs), default=None)
    complete = bool(
        snapshot_valid and positions_valid and len(returns_rows) >= 2
        and len(equity_rows) >= 2 and correlations_valid
    )
    if not complete:
        unknowns.append(DATA_GATED_METHODS["portfolio_risk"])
    monte_carlo = next(
        (
            item for item in reversed(tuple(context.get("concept_readings", ()) or ()))
            if isinstance(item, ConceptReading) and item.concept == "monte_carlo"
            and item.applicable is True
        ),
        None,
    )
    ruin = finite(monte_carlo.facts.get("probability_of_ruin")) if monte_carlo else None
    return ConceptReading(
        concept="portfolio_risk", timeframe=timeframe, domain=EvidenceDomain.PORTFOLIO,
        applicable=complete,
        applicability_reason=(
            "Whole-book open risk, exposure, synchronized return tail risk, drawdown and pairwise correlation are source-verified."
            if complete else
            "Portfolio risk remains partial until the whole-book broker snapshot and synchronized return/equity/correlation histories are complete."
        ),
        observation=f"{symbol} {timeframe}: whole-book viability evidence is separate from owner limits and execution authority.",
        implications=(),
        facts={
            "portfolio_id": portfolio_id or None,
            "portfolio_snapshot_verified": snapshot_valid,
            "positions_complete": positions_valid,
            "position_count": len(positions) if positions_valid else None,
            "account_equity": equity if snapshot_valid else None,
            "open_risk_account_ccy": open_risk,
            "live_heat_pct": heat,
            "gross_exposure_account_ccy": gross,
            "net_exposure_account_ccy": net,
            "currency_exposure_account_ccy": currencies if positions_valid else None,
            "portfolio_return_sample_count": len(returns),
            "portfolio_var_95_fraction": var_95,
            "portfolio_expected_shortfall_95_fraction": es_95,
            "portfolio_equity_curve_sample_count": len(equity_rows),
            "portfolio_max_drawdown_fraction": max_drawdown,
            "pairwise_correlations": pairs,
            "max_absolute_pairwise_correlation": max_corr,
            "monte_carlo_probability_of_ruin": ruin,
            "risk_of_ruin_available": ruin is not None,
            "directional_decision_emitted": False,
            "entry_authorization_emitted": False,
            "data_gated_reason": None if complete else DATA_GATED_METHODS["portfolio_risk"],
        },
        source_bar_epoch=cutoff, source_timestamp_epoch=snapshot_epoch,
        source_timestamp_verified=snapshot_valid,
        reference_close=reference, reference_timeframe=reference_tf,
        unknowns=tuple(dict.fromkeys(unknowns)),
        data_basis="broker_truth_whole_book_portfolio_risk",
        independence_group=f"portfolio_risk:{portfolio_id}" if portfolio_id else "",
    )


class PortfolioRiskReadings:
    key = "portfolio_risk_readings"
    family = "portfolio"
    covers_methods: tuple[str, ...] = ()
    covers_lenses: tuple[str, ...] = ()
    data_gated_methods = DATA_GATED_METHODS
    consumes_concept_readings = True

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        return self.read_with_context(symbol, bars_by_tf, {})

    def read_with_context(
        self, symbol: str, bars_by_tf: dict[str, list[dict]], context: dict[str, Any],
    ) -> list[ConceptReading]:
        reference, reference_tf = reference_price(bars_by_tf)
        resolved_context = dict(context or {})
        if not isinstance(resolved_context.get("portfolio_risk_snapshot"), dict):
            registry = resolved_context.get("data_source_registry")
            analysis_epoch = _epoch(resolved_context.get("analysis_epoch"))
            if registry is not None and analysis_epoch is not None:
                from data_sources.portfolio import build_portfolio_risk_snapshot
                resolved_context["portfolio_risk_snapshot"] = build_portfolio_risk_snapshot(
                    registry,
                    analysis_epoch=analysis_epoch,
                    completed_bar_epoch=last_source_epoch(
                        clean_bars((bars_by_tf or {}).get(reference_tf, []))
                    ),
                )
        readings: list[ConceptReading] = []
        for timeframe, raw in (bars_by_tf or {}).items():
            if timeframe not in TIMEFRAME_SECONDS:
                continue
            bars = clean_bars(raw)
            if bars:
                readings.append(_read_portfolio(
                    symbol, timeframe, bars, resolved_context, reference, reference_tf,
                ))
        return readings


__all__ = ["DATA_GATED_METHODS", "PortfolioRiskReadings"]
