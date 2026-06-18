"""Tests for the trading-loop hot-path performance optimizations.

Covers the behaviour-preserving fast paths added in the perf/trading-loop-hotpath
work that are testable without heavy deps (no torch/pandas required):
  * C7 — SignalLedger.record_signals batch insert + retention archive/prune.
  * ev_estimator memoization equivalence vs the original per-call computation.
  * platform_context.build_context_for_symbol caching returns identical, shared
    immutable contexts.
"""

import json
import time

import pytest

from adaptive.signal_ledger import SignalLedger, SignalRecord
from adaptive.ev_estimator import EVEstimator
import platform_context as pc


@pytest.fixture
def ledger(tmp_path):
    led = SignalLedger(
        db_path=tmp_path / "sig.db",
        grading_delay_minutes=0.0,
        check_intervals=[5, 15, 30],
        min_move_pct=0.1,
    )
    yield led
    led.close()


def _sig(pair="EURUSD", emitter="momentum", direction="LONG", price=100.0):
    return SignalRecord(
        pair=pair, emitter=emitter, direction=direction,
        strength=0.8, price_at_signal=price, context={"tf": "M5"},
    )


class TestBatchRecord:
    def test_record_signals_inserts_all_valid(self, ledger):
        sigs = [_sig(emitter=f"m{i}") for i in range(5)]
        written = ledger.record_signals(sigs)
        assert written == 5
        for s in sigs:
            assert ledger.get_signal(s.signal_id) is not None

    def test_record_signals_skips_neutral_and_empty(self, ledger):
        sigs = [
            _sig(emitter="a"),
            _sig(emitter="b", direction="NEUTRAL"),
            _sig(emitter="c", direction=""),
            _sig(emitter="d", direction="SHORT"),
        ]
        assert ledger.record_signals(sigs) == 2

    def test_record_signals_empty_input(self, ledger):
        assert ledger.record_signals([]) == 0

    def test_batch_matches_individual_record(self, ledger, tmp_path):
        # A batch insert yields the same stored rows as per-signal recording.
        other = SignalLedger(db_path=tmp_path / "sig2.db", grading_delay_minutes=0.0)
        try:
            s1 = _sig(pair="GBPUSD", emitter="structure")
            s2 = _sig(pair="GBPUSD", emitter="structure")
            ledger.record_signals([s1])
            other.record_signal(s2)
            a = ledger.get_signal(s1.signal_id)
            b = other.get_signal(s2.signal_id)
            assert a["pair"] == b["pair"] == "GBPUSD"
            assert a["emitter"] == b["emitter"] == "structure"
            assert a["direction"] == b["direction"]
        finally:
            other.close()


class TestRetention:
    def test_retention_archives_then_prunes(self, ledger, tmp_path, monkeypatch):
        import adaptive.signal_ledger as sl

        # Archive into the test's tmp dir, force retention to run, and treat
        # everything as "old".
        monkeypatch.setattr(sl, "_ARCHIVE_DIR", tmp_path / "archive")
        monkeypatch.setattr(sl, "_RETENTION_DAYS", 0)
        monkeypatch.setattr(sl, "_RETENTION_INTERVAL_S", 0.0)

        sid = ledger.record_signal(_sig(price=100.0))
        # One grading cycle finalises the signal (grading_delay=0) and then runs
        # retention (interval=0, days=0), which archives + prunes graded rows.
        ledger.run_grading_cycle({"EURUSD": 101.0})

        # Row pruned from the live DB...
        assert ledger.get_signal(sid) is None
        # ...and preserved in a JSONL archive.
        archive_files = list((tmp_path / "archive").glob("signal_ledger_*.jsonl"))
        assert archive_files, "expected an archive file"
        contents = archive_files[0].read_text().strip().splitlines()
        archived = [json.loads(line) for line in contents]
        assert any(r["signal_id"] == sid for r in archived)


class TestEVEstimatorCache:
    def _history(self):
        return [
            {"pair": "EURUSD", "regime": "trend", "session": "london",
             "pnl_dollars": 100.0, "risk_dollars": 50.0}
            for _ in range(15)
        ]

    def test_cache_returns_equal_result(self):
        est = EVEstimator(min_trades_for_gate=10)
        hist = self._history()
        first = est.estimate("EURUSD", "trend", "london", hist)
        second = est.estimate("EURUSD", "trend", "london", hist)
        # Same inputs → identical estimate (and served from cache the 2nd time).
        assert first == second
        assert first.expected_value == pytest.approx(2.0)  # 100/50 R, all wins

    def test_cache_invalidates_when_history_grows(self):
        est = EVEstimator(min_trades_for_gate=10)
        hist = self._history()
        est.estimate("EURUSD", "trend", "london", hist)
        hist.append({"pair": "EURUSD", "regime": "trend", "session": "london",
                     "pnl_dollars": -50.0, "risk_dollars": 50.0})
        updated = est.estimate("EURUSD", "trend", "london", hist)
        # A new (losing) trade must change the estimate, not serve a stale cache.
        assert updated.sample_size == 16
        assert updated.expected_value < 2.0

    def test_matches_naive_recomputation(self):
        est = EVEstimator(min_trades_for_gate=10)
        hist = [
            {"pair": "EURUSD", "regime": "trend", "session": "london",
             "pnl_dollars": (100.0 if i % 2 else -40.0), "risk_dollars": 50.0}
            for i in range(20)
        ]
        got = est.estimate("EURUSD", "trend", "london", hist)
        # Naive expected value over the same R-multiples.
        rs = [t["pnl_dollars"] / t["risk_dollars"] for t in hist]
        wins = [r for r in rs if r > 0]
        losses = [abs(r) for r in rs if r < 0]
        wr = len(wins) / len(rs)
        avg_w = sum(wins) / len(wins)
        avg_l = sum(losses) / len(losses)
        expected = round((wr * avg_w) - ((1 - wr) * avg_l), 4)
        assert got.expected_value == pytest.approx(expected)


class TestPlatformContextCache:
    def test_returns_same_immutable_instance(self):
        a = pc.build_context_for_symbol("EURUSD")
        b = pc.build_context_for_symbol("EURUSD")
        # Cached: identical object shared (safe because PlatformContext is frozen).
        assert a is b

    def test_override_bypasses_cache(self):
        cached = pc.build_context_for_symbol("EURUSD")
        custom = pc.build_context_for_symbol("EURUSD", broker="icmarkets")
        assert custom is not cached
        assert custom.broker == "icmarkets"
