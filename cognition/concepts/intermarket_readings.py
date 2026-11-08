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


def _verified_rows(frame: Any, symbol: str, timeframe: str, cutoff: float) -> list[dict[str, Any]]:
    """Convert provider frames using the same provenance rules as market-lens OHLC."""
    from cognition.market_lenses import _rows

    attrs = getattr(frame, "attrs", {})
    attrs = attrs if isinstance(attrs, dict) else {}
    rows = _rows(frame, limit=1_000_000, expected_symbol=symbol)
    # The live batch API returns a forming final bar. Exclude it unconditionally;
    # a row is also excluded unless its complete interval ended by the cycle cutoff.
    seconds = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}.get(timeframe)
    if not seconds:
        return []
    completed = []
    for row in rows[:-1]:
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
) -> tuple[list[float], list[float], list[float]]:
    target_by_time = {row["timestamp_epoch"]: row["close"] for row in target}
    peer_by_time = {row["timestamp_epoch"]: row["close"] for row in peer}
    common = sorted(set(target_by_time) & set(peer_by_time))
    return (
        common,
        [float(target_by_time[t]) for t in common],
        [float(peer_by_time[t]) for t in common],
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
        target_rows = [
            row for row in target
            if row.get("source_timestamp_verified") is True
            and _finite(row.get("timestamp_epoch", row.get("source_timestamp_epoch"))) is not None
        ]
        # Normalize the target timestamp field to the same representation as peers.
        for row in target_rows:
            row["timestamp_epoch"] = _finite(row.get("timestamp_epoch", row.get("source_timestamp_epoch")))
        peers: dict[str, list[dict[str, Any]]] = {}
        for raw_peer, peer_frames in frames.items():
            peer_symbol = str(raw_peer or "").strip().upper()
            if not peer_symbol or peer_symbol == target_symbol or not isinstance(peer_frames, dict):
                continue
            peer_rows = _verified_rows(peer_frames.get(timeframe), peer_symbol, timeframe, cutoff)
            if peer_rows:
                peers[peer_symbol] = peer_rows

        paired: dict[str, dict[str, Any]] = {}
        for peer_symbol, peer_rows in peers.items():
            times, target_closes, peer_closes = _aligned_closes(target_rows, peer_rows)
            target_returns, peer_returns = _returns(target_closes), _returns(peer_closes)
            correlation = _pearson(target_returns, peer_returns)
            if correlation is None:
                continue
            paired[peer_symbol] = {
                "times": times, "target_closes": target_closes,
                "peer_closes": peer_closes, "target_returns": target_returns,
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
            target_swing_high = _last_confirmed_swing(target_rows, "high")
            peer_swing_high = _last_confirmed_swing(peer_rows, "high")
            target_swing_low = _last_confirmed_swing(target_rows, "low")
            peer_swing_low = _last_confirmed_swing(peer_rows, "low")
            target_last, peer_last = target_rows[-1], peer_rows[-1]
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
                 "classification": smt, "paired_return_count": peer["sample_count"]},
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

        # Currency strength requires explicit verified contract metadata from the adapter.
        specs = context.get("multi_symbol_specs")
        if isinstance(specs, dict) and paired:
            currency_values: dict[str, list[float]] = {}
            accepted_symbols = []
            for peer_symbol, item in paired.items():
                spec = specs.get(peer_symbol)
                if not isinstance(spec, dict) or spec.get("source_verified") is not True or spec.get("specification_verified") is not True:
                    continue
                base = str(spec.get("currency_base") or "").strip().upper()
                quote = str(spec.get("currency_profit") or "").strip().upper()
                if not base or not quote or base == quote:
                    continue
                returns = item["target_returns"]
                # A peer's own returns are required; calculate them from aligned closes.
                peer_returns = item["peer_returns"]
                if not peer_returns:
                    continue
                currency_values.setdefault(base, []).extend(peer_returns)
                currency_values.setdefault(quote, []).extend(-value for value in peer_returns)
                accepted_symbols.append(peer_symbol)
            scores = {
                currency: sum(values) / len(values)
                for currency, values in currency_values.items() if values
            }
            if len(scores) >= 2:
                ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
                output.append(_reading(
                    "currency_strength", target_symbol, timeframe, latest_common_epoch,
                    _finite(target[-1].get("close")),
                    f"Relative currency movement is measured across {len(accepted_symbols)} verified instrument specifications.",
                    {"currency_scores": dict(ranked), "ranked_currencies": [key for key, _ in ranked],
                     "contributing_symbols": accepted_symbols,
                     "metadata_basis": "broker-attested base and profit currency fields"},
                    _DATA_REQUIREMENTS["currency_strength"],
                ))
        return output
