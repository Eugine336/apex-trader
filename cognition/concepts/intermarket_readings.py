"""Broker-neutral intermarket readings over one synchronized completed-bar snapshot.

The reader consumes normalized frames supplied by any broker adapter. It does
not select a trade or infer missing peer data. Correlation is descriptive;
relative-value stationarity and SMT event interpretation remain explicitly
unconfirmed until their independent acceptance tests pass.
"""
from __future__ import annotations

import math
from typing import Any

from cognition.concepts.auction_utils import clean_bars, last_source_epoch
from cognition.concepts.base import FLAT, ConceptReading, Implication, reference_price
from cognition.contracts import EvidenceDomain

_DATA_REQUIREMENTS = {
    "cross_asset": "source-verified multi-symbol completed bars aligned to one decision cutoff, with stable instrument identity and sufficient paired history",
    "currency_strength": "source-verified synchronized bars and verified base/quote currency metadata for the enabled instrument universe",
    "smt_divergence": "synchronized completed bars for a measured correlated-instrument pair and confirmed completed swing/sweep events",
    "relative_value": "source-verified synchronized pair histories plus independently tested spread stationarity and relationship stability",
}


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        return None
    lm, rm = sum(left) / len(left), sum(right) / len(right)
    lc = [v - lm for v in left]
    rc = [v - rm for v in right]
    denom = math.sqrt(sum(v * v for v in lc) * sum(v * v for v in rc))
    if denom <= 0:
        return None
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(lc, rc)) / denom))


def _verified_rows(
    frame: Any, symbol: str, timeframe: str, cutoff: float,
    *, includes_forming_bar: bool,
) -> list[dict[str, Any]]:
    """Convert provider frames using the same provenance rules as market-lens OHLC."""
    from cognition.market_lenses import _rows

    rows = _rows(frame, limit=1_000_000, expected_symbol=symbol)
    # The live batch API returns a forming final bar. Exclude it unconditionally;
    # a row is also excluded unless its complete interval ended by the cycle cutoff.
    seconds = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}.get(timeframe)
    if not seconds:
        return []
    completed = []
    source_rows = rows[:-1] if includes_forming_bar and rows else rows
    for row in source_rows:
        stamp = _finite(row.get("timestamp_epoch"))
        if (
            row.get("source_timestamp_verified") is True
            and stamp is not None
            and stamp + seconds <= cutoff
            and row.get("source_broker")
            and str(row.get("source_timestamp_origin") or "") == "broker_ohlcv"
        ):
            completed.append({
                "timestamp_epoch": stamp,
                "open": row["open"], "high": row["high"],
                "low": row["low"], "close": row["close"],
                "source_timestamp_verified": True,
            })
    return completed


def _aligned_closes(
    target: list[dict[str, Any]], peer: list[dict[str, Any]],
    timeframe_seconds: int,
) -> tuple[list[float], list[float], list[float], list[float], list[float]]:
    """Return common price levels and returns only across adjacent common bars."""
    target_by_time = {row["timestamp_epoch"]: row["close"] for row in target}
    peer_by_time = {row["timestamp_epoch"]: row["close"] for row in peer}
    common = sorted(set(target_by_time) & set(peer_by_time))
    return_times: list[float] = []
    target_returns: list[float] = []
    peer_returns: list[float] = []
    for previous, current in zip(common, common[1:]):
        if current - previous != timeframe_seconds:
            continue
        target_previous, target_current = float(target_by_time[previous]), float(target_by_time[current])
        peer_previous, peer_current = float(peer_by_time[previous]), float(peer_by_time[current])
        if min(target_previous, target_current, peer_previous, peer_current) <= 0:
            continue
        return_times.append(current)
        target_returns.append(math.log(target_current / target_previous))
        peer_returns.append(math.log(peer_current / peer_previous))
    return (
        common,
        [float(target_by_time[t]) for t in common],
        [float(peer_by_time[t]) for t in common],
        return_times, target_returns, peer_returns,
    )


def _returns(values: list[float]) -> list[float]:
    result = []
    for previous, current in zip(values, values[1:]):
        if previous <= 0 or current <= 0:
            return []
        result.append(math.log(current / previous))
    return result


def _last_confirmed_swing(rows: list[dict[str, Any]], side: str) -> float | None:
    """Return the most recent three-bar fractal swing, confirmed by closed bars."""
    if len(rows) < 3:
        return None
    key = "high" if side == "high" else "low"
    candidates = []
    # A three-bar fractal is a structural definition, not a tunable threshold.
    for index in range(1, len(rows) - 1):
        value = _finite(rows[index].get(key))
        left = _finite(rows[index - 1].get(key))
        right = _finite(rows[index + 1].get(key))
        if value is None or left is None or right is None:
            continue
        if (value > left and value > right) if side == "high" else (value < left and value < right):
            candidates.append(value)
    return candidates[-1] if candidates else None


def _reading(
    concept: str, symbol: str, timeframe: str, epoch: float | None,
    reference: float | None, observation: str, facts: dict[str, Any],
    requirement: str, *, unknowns: tuple[str, ...] = (),
) -> ConceptReading:
    return ConceptReading(
        concept=concept,
        timeframe=timeframe,
        domain=EvidenceDomain.CORRELATION,
        applicable=True,
        applicability_reason=f"{concept} is assessed from the available synchronized broker-neutral observations.",
        observation=observation,
        implications=(Implication(
            direction=FLAT,
            statement="This is relational market evidence for the Brain to assess; it is not a directional vote.",
            strength=None,
            activated=False,
            activation_reason="No standalone trade trigger is produced by an intermarket measurement.",
            requires=(requirement,) if unknowns else (),
            conflicts_with=("Correlation and relative movement do not establish causation.",),
            management={"meaning": "Contextual evidence only; the Brain retains all decision authority."},
        ),),
        facts=facts,
        source_bar_epoch=epoch,
        reference_close=reference,
        reference_timeframe=timeframe,
        unknowns=unknowns,
        data_basis="broker-neutral synchronized completed OHLC bars",
        independence_group="intermarket_relationships",
        source_timestamp_epoch=epoch,
        source_timestamp_verified=epoch is not None,
    )


class IntermarketReadings:
    key = "intermarket_readings"
    family = "intermarket"
    covers_methods: tuple[str, ...] = ()
    covers_lenses: tuple[str, ...] = ()
    data_gated_methods = _DATA_REQUIREMENTS

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        return []

    def read_with_context(
        self, symbol: str, bars_by_tf: dict[str, list[dict]], context: dict[str, Any],
    ) -> list[ConceptReading]:
        target_symbol = str(symbol or "").strip().upper()
        frames = context.get("multi_symbol_frames")
        cutoff = _finite(context.get("multi_symbol_snapshot_cutoff_epoch"))
        if not isinstance(frames, dict) or cutoff is None:
            return []
        _, timeframe = reference_price(bars_by_tf)
        if not timeframe:
            return []
        target = clean_bars((bars_by_tf or {}).get(timeframe, []))
        if not target or target[-1].get("source_timestamp_verified") is not True:
            return []
        target_epoch = last_source_epoch(target)
        if target_epoch is None:
            return []
        # The current symbol's own bars are already normalized by market_lenses.
        seconds = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}.get(timeframe)
        if not seconds:
            return []
        target_rows = [
            row for row in target
            if row.get("source_timestamp_verified") is True
            and _finite(row.get("timestamp_epoch", row.get("source_timestamp_epoch"))) is not None
            and _finite(row.get("timestamp_epoch", row.get("source_timestamp_epoch"))) + seconds <= cutoff
            and row.get("source_broker")
        ]
        # Normalize the target timestamp field to the same representation as peers.
        for row in target_rows:
            row["timestamp_epoch"] = _finite(row.get("timestamp_epoch", row.get("source_timestamp_epoch")))
        if len(target_rows) < 3:
            return []
        includes_forming_bar = context.get("multi_symbol_source_includes_forming_bar") is True
        peers: dict[str, list[dict[str, Any]]] = {}
        for raw_peer, peer_frames in frames.items():
            peer_symbol = str(raw_peer or "").strip().upper()
            if not peer_symbol or peer_symbol == target_symbol or not isinstance(peer_frames, dict):
                continue
            peer_rows = _verified_rows(
                peer_frames.get(timeframe), peer_symbol, timeframe, cutoff,
                includes_forming_bar=includes_forming_bar,
            )
            if peer_rows:
                peers[peer_symbol] = peer_rows

        paired: dict[str, dict[str, Any]] = {}
        for peer_symbol, peer_rows in peers.items():
            times, target_closes, peer_closes, return_times, target_returns, peer_returns = _aligned_closes(target_rows, peer_rows, seconds)
            correlation = _pearson(target_returns, peer_returns)
            if correlation is None:
                continue
            paired[peer_symbol] = {
                "times": times, "target_closes": target_closes,
                "peer_closes": peer_closes, "return_times": return_times,
                "target_returns": target_returns,
                "peer_returns": peer_returns, "correlation": correlation,
                "sample_count": len(target_returns),
            }
        latest_common_epoch = max(
            (max(item["times"]) for item in paired.values() if item["times"]),
            default=target_epoch,
        )
        output: list[ConceptReading] = []
        requirement = _DATA_REQUIREMENTS["cross_asset"]
        if paired:
            ordered = sorted(paired.items(), key=lambda item: abs(item[1]["correlation"]), reverse=True)
            output.append(_reading(
                "cross_asset", target_symbol, timeframe, latest_common_epoch,
                _finite(target[-1].get("close")),
                f"{target_symbol} has synchronized return relationships with {len(paired)} peer instruments on {timeframe}.",
                {"peer_relationships": [
                    {"symbol": peer, "return_correlation": item["correlation"],
                     "paired_return_count": item["sample_count"],
                     "common_bar_count": len(item["times"])}
                    for peer, item in ordered
                ], "selection_basis": "absolute Pearson return correlation across exact common source timestamps",
                   "snapshot_cutoff_epoch": cutoff},
                requirement,
            ))

            # SMT: select the most strongly related peer from measured returns,
            # then compare confirmed three-bar swing sweeps on completed bars.
            peer_symbol, peer = ordered[0]
            peer_rows = peers[peer_symbol]
            # Structural comparison is restricted to the same final common bar.
            common_epoch = max(
                set(row["timestamp_epoch"] for row in target_rows)
                & set(row["timestamp_epoch"] for row in peer_rows),
                default=None,
            )
            target_aligned = [row for row in target_rows if row["timestamp_epoch"] <= common_epoch] if common_epoch is not None else []
            peer_aligned = [row for row in peer_rows if row["timestamp_epoch"] <= common_epoch] if common_epoch is not None else []
            target_swing_high = _last_confirmed_swing(target_aligned, "high")
            peer_swing_high = _last_confirmed_swing(peer_aligned, "high")
            target_swing_low = _last_confirmed_swing(target_aligned, "low")
            peer_swing_low = _last_confirmed_swing(peer_aligned, "low")
            target_last = target_aligned[-1] if target_aligned else {}
            peer_last = peer_aligned[-1] if peer_aligned else {}
            bearish_target = target_swing_high is not None and target_last["high"] > target_swing_high and target_last["close"] < target_swing_high
            bearish_peer = peer_swing_high is not None and peer_last["high"] > peer_swing_high and peer_last["close"] < peer_swing_high
            bullish_target = target_swing_low is not None and target_last["low"] < target_swing_low and target_last["close"] > target_swing_low
            bullish_peer = peer_swing_low is not None and peer_last["low"] < peer_swing_low and peer_last["close"] > peer_swing_low
            smt = (
                "bearish_divergence" if bearish_target and not bearish_peer
                else "bullish_divergence" if bullish_target and not bullish_peer
                else "no_confirmed_divergence"
            )
            output.append(_reading(
                "smt_divergence", target_symbol, timeframe, target_epoch,
                _finite(target[-1].get("close")),
                f"SMT comparison against measured peer {peer_symbol}: {smt.replace('_', ' ')}.",
                {"peer_symbol": peer_symbol, "peer_return_correlation": peer["correlation"],
                 "target_swing_high": target_swing_high, "peer_swing_high": peer_swing_high,
                 "target_swing_low": target_swing_low, "peer_swing_low": peer_swing_low,
                 "target_bearish_sweep": bearish_target, "peer_bearish_sweep": bearish_peer,
                 "target_bullish_sweep": bullish_target, "peer_bullish_sweep": bullish_peer,
                 "classification": smt, "paired_return_count": peer["sample_count"],
                 "comparison_epoch": common_epoch},
                _DATA_REQUIREMENTS["smt_divergence"],
                unknowns=() if smt != "no_confirmed_divergence" else ("confirmed asymmetric sweep event on completed bars",),
            ))

            # Relative value: OLS residual and its observed standardized position.
            # This is descriptive only; a formal stationarity test is still required.
            x, y = peer["peer_closes"], peer["target_closes"]
            xm, ym = sum(x) / len(x), sum(y) / len(y)
            variance = sum((value - xm) ** 2 for value in x)
            beta = sum((a - xm) * (b - ym) for a, b in zip(x, y)) / variance if variance > 0 else None
            alpha = ym - beta * xm if beta is not None else None
            residuals = [b - (alpha + beta * a) for a, b in zip(x, y)] if beta is not None else []
            residual_mean = sum(residuals) / len(residuals) if residuals else None
            residual_std = math.sqrt(sum((v - residual_mean) ** 2 for v in residuals) / (len(residuals) - 1)) if len(residuals) > 1 else None
            residual_z = (residuals[-1] - residual_mean) / residual_std if residual_std and residual_std > 0 else None
            output.append(_reading(
                "relative_value", target_symbol, timeframe, latest_common_epoch,
                _finite(target[-1].get("close")),
                f"{target_symbol}/{peer_symbol} synchronized spread residual is measured; stationarity is not yet established.",
                {"peer_symbol": peer_symbol, "return_correlation": peer["correlation"],
                 "regression_intercept": alpha, "regression_beta": beta,
                 "latest_residual": residuals[-1] if residuals else None,
                 "residual_zscore": residual_z, "residual_observation_count": len(residuals),
                 "stationarity_test_performed": False, "relationship_stability_verified": False},
                _DATA_REQUIREMENTS["relative_value"],
                unknowns=("formal residual stationarity and relationship-stability validation",),
            ))

        # Currency strength uses only the exact intersection of return timestamps
        # across every included instrument. No peer's missing interval is imputed.
        specs = context.get("multi_symbol_specs")
        if isinstance(specs, dict) and paired:
            first_pair = next(iter(paired.values()))
            instrument_series: dict[str, dict[float, float]] = {}
            target_spec = specs.get(target_symbol)
            if (
                isinstance(target_spec, dict)
                and target_spec.get("source_verified") is True
                and target_spec.get("specification_verified") is True
            ):
                instrument_series[target_symbol] = dict(zip(
                    first_pair["return_times"], first_pair["target_returns"],
                ))
            for peer_symbol, item in paired.items():
                spec = specs.get(peer_symbol)
                if (
                    isinstance(spec, dict)
                    and spec.get("source_verified") is True
                    and spec.get("specification_verified") is True
                ):
                    instrument_series[peer_symbol] = dict(zip(
                        item["return_times"], item["peer_returns"],
                    ))
            common_return_times = (
                set.intersection(*(set(values) for values in instrument_series.values()))
                if instrument_series else set()
            )
            currency_values: dict[str, list[float]] = {}
            accepted_symbols: list[str] = []
            for instrument_symbol, series in instrument_series.items():
                spec = specs[instrument_symbol]
                base = str(spec.get("currency_base") or "").strip().upper()
                quote = str(spec.get("currency_profit") or "").strip().upper()
                if not base or not quote or base == quote or not common_return_times:
                    continue
                for stamp in sorted(common_return_times):
                    change = series[stamp]
                    currency_values.setdefault(base, []).append(change)
                    currency_values.setdefault(quote, []).append(-change)
                accepted_symbols.append(instrument_symbol)
            scores = {
                currency: sum(values) / len(values)
                for currency, values in currency_values.items() if values
            }
            if len(scores) >= 2 and accepted_symbols:
                ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
                output.append(_reading(
                    "currency_strength", target_symbol, timeframe, max(common_return_times),
                    _finite(target[-1].get("close")),
                    f"Relative currency movement is measured across {len(accepted_symbols)} verified instruments on {len(common_return_times)} common return intervals.",
                    {"currency_scores": dict(ranked), "ranked_currencies": [key for key, _ in ranked],
                     "contributing_symbols": accepted_symbols,
                     "common_return_interval_count": len(common_return_times),
                     "metadata_basis": "broker-attested base and profit currency fields"},
                    _DATA_REQUIREMENTS["currency_strength"],
                ))
        return output
