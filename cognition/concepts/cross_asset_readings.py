"""Synchronized multi-symbol OHLC relationship readings.

Pair selection is measured from the current common-timestamp history. No fixed
symbol-pair table, directional vote or trade authority is introduced.
"""
from __future__ import annotations

from math import isfinite, log, sqrt
from typing import Any

from cognition.concepts.auction_utils import clean_bars
from cognition.concepts.base import TIMEFRAME_SECONDS, ConceptReading, finite
from cognition.contracts import EvidenceDomain


MIN_RETURN_SAMPLES = 30
MAX_PEER_READINGS = 10


def _verified_by_epoch(rows: Any) -> dict[float, dict[str, Any]]:
    result: dict[float, dict[str, Any]] = {}
    for bar in clean_bars(rows):
        epoch = finite(bar.get("timestamp_epoch"))
        close = finite(bar.get("close"))
        if (
            epoch is None or epoch <= 0.0 or close is None or close <= 0.0
            or bar.get("source_timestamp_verified") is not True
        ):
            continue
        result[epoch] = bar
    return result


def _paired_returns(
    target: dict[float, dict[str, Any]],
    peer: dict[float, dict[str, Any]],
    timeframe_seconds: int,
) -> tuple[list[float], list[float], list[float]]:
    common = sorted(set(target) & set(peer))
    target_returns: list[float] = []
    peer_returns: list[float] = []
    return_epochs: list[float] = []
    # Session/weekend gaps are not treated as one ordinary intraday bar.
    max_gap = timeframe_seconds * (3 if timeframe_seconds >= 86_400 else 2)
    for previous_epoch, current_epoch in zip(common, common[1:]):
        if current_epoch - previous_epoch > max_gap:
            continue
        target_previous = target[previous_epoch]["close"]
        target_current = target[current_epoch]["close"]
        peer_previous = peer[previous_epoch]["close"]
        peer_current = peer[current_epoch]["close"]
        if min(target_previous, target_current, peer_previous, peer_current) <= 0.0:
            continue
        target_returns.append(log(target_current / target_previous))
        peer_returns.append(log(peer_current / peer_previous))
        return_epochs.append(current_epoch)
    return target_returns, peer_returns, return_epochs


def _correlation(x: list[float], y: list[float]) -> float | None:
    if len(x) != len(y) or len(x) < 2:
        return None
    mean_x = sum(x) / len(x)
    mean_y = sum(y) / len(y)
    dx = [value - mean_x for value in x]
    dy = [value - mean_y for value in y]
    variance_x = sum(value * value for value in dx)
    variance_y = sum(value * value for value in dy)
    if variance_x <= 0.0 or variance_y <= 0.0:
        return None
    value = sum(a * b for a, b in zip(dx, dy)) / sqrt(variance_x * variance_y)
    return max(-1.0, min(1.0, value)) if isfinite(value) else None


def _beta(target: list[float], peer: list[float]) -> float | None:
    if len(target) != len(peer) or len(target) < 2:
        return None
    mean_target = sum(target) / len(target)
    mean_peer = sum(peer) / len(peer)
    variance_peer = sum((value - mean_peer) ** 2 for value in peer)
    if variance_peer <= 0.0:
        return None
    covariance = sum(
        (left - mean_target) * (right - mean_peer)
        for left, right in zip(target, peer)
    )
    value = covariance / variance_peer
    return value if isfinite(value) else None


class CrossAssetReadings:
    """Read pairwise dependence from synchronized completed-bar histories."""

    key = "cross_asset_readings"
    family = "intermarket"
    covers_methods = ("cross_asset",)
    covers_lenses: tuple[str, ...] = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        return []

    def read_with_context(
        self, symbol: str, bars_by_tf: dict[str, list[dict]], context: dict[str, Any],
    ) -> list[ConceptReading]:
        target_symbol = str(symbol or "").strip().upper()
        related = context.get("related_bars_by_symbol", {})
        related = related if isinstance(related, dict) else {}
        readings: list[ConceptReading] = []

        for timeframe, target_raw in (bars_by_tf or {}).items():
            if timeframe not in TIMEFRAME_SECONDS:
                continue
            target = _verified_by_epoch(target_raw)
            if not target:
                continue
            peers: list[dict[str, Any]] = []
            for peer_symbol, peer_frames in related.items():
                peer = str(peer_symbol or "").strip().upper()
                if not peer or peer == target_symbol or not isinstance(peer_frames, dict):
                    continue
                peer = peer.upper()
                peer_rows = _verified_by_epoch(peer_frames.get(timeframe, []))
                if not peer_rows:
                    continue
                x, y, epochs = _paired_returns(
                    target, peer_rows, TIMEFRAME_SECONDS[timeframe],
                )
                # _paired_returns expects timestamp-indexed maps; preserve exact
                # common timestamps and never forward-fill or interpolate.
                if len(x) < MIN_RETURN_SAMPLES:
                    continue
                correlation = _correlation(x[-120:], y[-120:])
                if correlation is None:
                    continue
                window = min(len(x), 120)
                selected_x, selected_y = x[-window:], y[-window:]
                rolling_20 = _correlation(x[-20:], y[-20:]) if len(x) >= 20 else None
                rolling_60 = _correlation(x[-60:], y[-60:]) if len(x) >= 60 else None
                rolling_120 = _correlation(x[-120:], y[-120:]) if len(x) >= 120 else None
                latest_epoch = epochs[-1]
                target_close = target[latest_epoch]["close"]
                peer_close = peer_rows[latest_epoch]["close"]
                peers.append({
                    "peer_symbol": peer,
                    "correlation": correlation,
                    "beta_target_on_peer": _beta(selected_x, selected_y),
                    "rolling_correlation_20": rolling_20,
                    "rolling_correlation_60": rolling_60,
                    "rolling_correlation_120": rolling_120,
                    "relative_log_return": sum(selected_x) - sum(selected_y),
                    "sample_count": window,
                    "latest_common_epoch": latest_epoch,
                    "target_close": target_close,
                    "peer_close": peer_close,
                })

            peers.sort(key=lambda item: abs(item["correlation"]), reverse=True)
            for rank, item in enumerate(peers[:MAX_PEER_READINGS], start=1):
                peer = item["peer_symbol"]
                correlation = item["correlation"]
                readings.append(ConceptReading(
                    concept="cross_asset",
                    timeframe=timeframe,
                    domain=EvidenceDomain.CORRELATION,
                    applicable=True,
                    applicability_reason=(
                        f"{item['sample_count']} exact, source-verified common return pairs "
                        f"are available for {target_symbol}/{peer}."
                    ),
                    observation=(
                        f"{target_symbol}/{peer} rolling log-return correlation="
                        f"{correlation:.4f} over {item['sample_count']} common returns; "
                        "this is descriptive dependence, not causation or directional authority."
                    ),
                    implications=(),
                    facts={
                        "evidence_source_module": (
                            f"concept.cross_asset.{target_symbol.lower()}."
                            f"{peer.lower()}.{timeframe.lower()}"
                        ),
                        "target_symbol": target_symbol,
                        "peer_symbol": peer,
                        "measured_pair_rank": rank,
                        "pair_selection_basis": "absolute_correlation_from_synchronized_history",
                        "correlation": correlation,
                        "beta_target_on_peer": item["beta_target_on_peer"],
                        "rolling_correlation_20": item["rolling_correlation_20"],
                        "rolling_correlation_60": item["rolling_correlation_60"],
                        "rolling_correlation_120": item["rolling_correlation_120"],
                        "relative_log_return": item["relative_log_return"],
                        "sample_count": item["sample_count"],
                        "latest_common_epoch": item["latest_common_epoch"],
                        "target_close": item["target_close"],
                        "peer_close": item["peer_close"],
                        "minimum_return_samples": MIN_RETURN_SAMPLES,
                        "maximum_peer_readings": MAX_PEER_READINGS,
                        "causation_established": False,
                        "directional_decision_emitted": False,
                    },
                    source_bar_epoch=item["latest_common_epoch"],
                    source_timestamp_epoch=item["latest_common_epoch"],
                    source_timestamp_verified=True,
                    reference_close=item["target_close"],
                    reference_timeframe=timeframe,
                    unknowns=(
                        "Correlation and beta describe historical co-movement; they do not establish causation.",
                        "No fundamental, policy, contract or transmission mechanism is inferred.",
                        "Peer ranking is sample-dependent and may change as synchronized observations arrive.",
                    ),
                    data_basis="synchronized_multi_symbol_completed_ohlc",
                    independence_group=f"intermarket:{target_symbol}:{peer}:{timeframe}",
                    relevance_horizon_seconds=3 * TIMEFRAME_SECONDS[timeframe],
                ))

            if not peers:
                latest_epoch = max(target)
                readings.append(ConceptReading(
                    concept="cross_asset",
                    timeframe=timeframe,
                    domain=EvidenceDomain.CORRELATION,
                    applicable=False,
                    applicability_reason="No peer has enough exact, source-verified common returns.",
                    observation=(
                        f"No synchronized peer relationship can currently be estimated for "
                        f"{target_symbol} on {timeframe}."
                    ),
                    implications=(),
                    facts={
                        "target_symbol": target_symbol,
                        "peer_count_received": len(related),
                        "minimum_return_samples": MIN_RETURN_SAMPLES,
                        "directional_decision_emitted": False,
                    },
                    source_bar_epoch=latest_epoch,
                    reference_close=target[latest_epoch]["close"],
                    reference_timeframe=timeframe,
                    unknowns=(
                        "Synchronized peer-symbol history is unavailable or shorter than the declared sample floor.",
                        "No fixed pair list or unsynchronized substitute is used.",
                    ),
                    data_basis="synchronized_multi_symbol_completed_ohlc",
                    relevance_horizon_seconds=3 * TIMEFRAME_SECONDS[timeframe],
                ))
        return readings


__all__ = ["CrossAssetReadings", "MIN_RETURN_SAMPLES", "MAX_PEER_READINGS"]
