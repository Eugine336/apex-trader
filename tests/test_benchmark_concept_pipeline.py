from __future__ import annotations

from scripts.benchmark_concept_pipeline import run_benchmark, summarize


class _Manager:
    def __init__(self):
        self.fetch_calls = 0

    def fetch_all_market_data(self, *, symbols, timeframes, count):
        self.fetch_calls += 1
        return {
            symbol: {tf: [object()] * count for tf in timeframes}
            for symbol in symbols
        }


def test_summary_reports_incomplete_universe_instead_of_hiding_missing_symbols():
    result = summarize(
        [{"symbol": "EURUSD", "elapsed_ms": 10.0}],
        ["EURUSD", "GBPUSD"],
        ["M5", "H1"],
    )
    assert result["requested_symbol_count"] == 2
    assert result["completed_symbol_count"] == 1
    assert result["missing_symbols"] == ["GBPUSD"]
    assert result["per_symbol_ms"]["p95"] == 10.0


def test_benchmark_runs_cold_and_warm_on_one_immutable_market_snapshot():
    manager = _Manager()
    calls = []

    def analyze(symbol, frames, *, source_includes_forming_bar):
        calls.append((symbol, id(frames), source_includes_forming_bar))
        return [object(), object()]

    report = run_benchmark(
        manager, ["EURUSD", "GBPUSD"], ["M5", "H1"], 20,
        analyze=analyze,
    )
    assert manager.fetch_calls == 1
    assert len(calls) == 4
    assert calls[0][1] == calls[2][1]
    assert calls[1][1] == calls[3][1]
    assert all(item[2] is True for item in calls)
    assert report["cold"]["completed_symbol_count"] == 2
    assert report["warm"]["completed_symbol_count"] == 2
    assert report["execution_mutations_performed"] == 0


def test_benchmark_records_analysis_faults_without_claiming_complete_coverage():
    manager = _Manager()
    calls = []

    def analyze(symbol, frames, *, source_includes_forming_bar):
        calls.append(symbol)
        if symbol == "GBPUSD":
            raise RuntimeError("reader pipeline fault")
        return [object()]

    report = run_benchmark(
        manager, ["EURUSD", "GBPUSD"], ["M5"], 10, analyze=analyze,
    )
    assert report["cold"]["completed_symbol_count"] == 1
    assert report["cold"]["failures"] == [{
        "symbol": "GBPUSD",
        "exception_type": "RuntimeError",
        "error": "reader pipeline fault",
    }]
    assert report["warm"]["failures"] == report["cold"]["failures"]


def test_summary_reports_missing_timeframes_even_when_symbol_is_present():
    result = summarize(
        [{
            "symbol": "EURUSD",
            "elapsed_ms": 10.0,
            "timeframes": {"M5": 20},
        }],
        ["EURUSD"],
        ["M5", "H1"],
    )
    assert result["completed_symbol_count"] == 1
    assert result["completed_symbol_timeframe_pairs"] == 1
    assert result["expected_symbol_timeframe_pairs"] == 2
    assert result["incomplete_timeframes"] == [{
        "symbol": "EURUSD",
        "missing": ["H1"],
    }]
