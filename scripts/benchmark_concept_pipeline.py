"""Measure the live concept pipeline across the configured broker universe.

Read-only: connects for market-data access, fetches OHLCV and calls
analyze_market_frames. It never creates intents or invokes broker mutations.
Run on the trading host to produce an actual broker-universe baseline.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import datetime, timezone
from typing import Any


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def summarize(
    samples: list[dict[str, Any]],
    requested_symbols: list[str],
    failures: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    durations = [float(row["elapsed_ms"]) for row in samples]
    completed = {str(row["symbol"]).upper() for row in samples}
    failed_symbols = {
        str(row.get("symbol", "")).upper() for row in (failures or [])
    }
    return {
        "sample_count": len(samples),
        "requested_symbol_count": len(requested_symbols),
        "completed_symbol_count": len(completed),
        "missing_symbols": sorted(set(map(str.upper, requested_symbols)) - completed),
        "failures": list(failures or []),
        "total_elapsed_ms": round(sum(durations), 3),
        "per_symbol_ms": {
            "min": round(min(durations), 3) if durations else None,
            "median": round(statistics.median(durations), 3) if durations else None,
            "p95": round(_percentile(durations, 0.95), 3) if durations else None,
            "max": round(max(durations), 3) if durations else None,
        },
        "samples": samples,
    }


def run_benchmark(
    manager: Any,
    symbols: list[str],
    timeframes: list[str],
    count: int,
    *,
    analyze: Any,
) -> dict[str, Any]:
    market_data = manager.fetch_all_market_data(
        symbols=symbols, timeframes=timeframes, count=count,
    )
    passes: dict[str, list[dict[str, Any]]] = {"cold": [], "warm": []}
    failures: dict[str, list[dict[str, str]]] = {"cold": [], "warm": []}
    for pass_name in ("cold", "warm"):
        if pass_name == "warm":
            # Same completed-bar snapshot deliberately exercises the full-history
            # reader cache without introducing new broker or market inputs.
            pass
        for symbol in symbols:
            frames = market_data.get(symbol)
            if not frames:
                continue
            started = time.perf_counter()
            try:
                evidence = analyze(
                    symbol, frames, source_includes_forming_bar=True,
                )
            except Exception as exc:
                failures[pass_name].append({
                    "symbol": symbol,
                    "exception_type": type(exc).__name__,
                    "error": str(exc)[:500],
                })
                continue
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            passes[pass_name].append({
                "symbol": symbol,
                "elapsed_ms": round(elapsed_ms, 3),
                "timeframes": {
                    tf: int(len(frame)) for tf, frame in frames.items()
                    if frame is not None
                },
                "evidence_count": len(evidence),
            })
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timeframes": list(timeframes),
        "bars_requested_per_timeframe": count,
        "market_data_symbol_count": len(market_data),
        "cold": summarize(passes["cold"], symbols, failures["cold"]),
        "warm": summarize(passes["warm"], symbols, failures["warm"]),
        "cache_comparison": {
            "cold_total_ms": round(sum(row["elapsed_ms"] for row in passes["cold"]), 3),
            "warm_total_ms": round(sum(row["elapsed_ms"] for row in passes["warm"]), 3),
            "warm_to_cold_ratio": (
                round(
                    sum(row["elapsed_ms"] for row in passes["warm"])
                    / sum(row["elapsed_ms"] for row in passes["cold"]),
                    4,
                )
                if sum(row["elapsed_ms"] for row in passes["cold"]) > 0 else None
            ),
        },
        "execution_mutations_performed": 0,
        "market_data_source": "connected broker adapters",
        "benchmark_scope": "configured instrument universe; same immutable OHLC snapshot for cold/warm comparison",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--timeframes", nargs="+", default=["M5", "M15", "H1", "H4"])
    parser.add_argument("--symbols", nargs="*", default=None)
    args = parser.parse_args(argv)
    if args.count < 1:
        parser.error("--count must be positive")

    from cognition.market_lenses import analyze_market_frames
    from platforms.platform_manager import PlatformManager

    manager = PlatformManager()
    requested = list(args.symbols or manager.config.enabled_pairs)
    if not requested:
        parser.error("configured instrument universe is empty")
    connected = manager.connect_all()
    if not any(connected.values()):
        manager.disconnect_all()
        print(json.dumps({
            "status": "no_market_data_platform_connected",
            "connected_platforms": connected,
            "execution_mutations_performed": 0,
        }, indent=2))
        return 2
    try:
        report = run_benchmark(
            manager, requested, [str(tf).upper() for tf in args.timeframes],
            args.count, analyze=analyze_market_frames,
        )
        report["connected_platforms"] = connected
        report["status"] = (
            "complete"
            if report["cold"]["completed_symbol_count"] == len(requested)
            and not report["cold"]["failures"]
            and not report["warm"]["failures"]
            else "partial"
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["status"] == "complete" else 2
    finally:
        manager.disconnect_all()


if __name__ == "__main__":
    raise SystemExit(main())
