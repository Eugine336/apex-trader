"""
Tests for the Post-Close Price Tracker (Learning Layer Session 2).

Covers the pure excursion/classification math, the full scheduling lifecycle
through a fake data source + controllable clock, SQLite persistence and reload
across a restart, the learner accessors, and the edge cases that must never
stall the scan loop (price-fetch failure, offline pair, missing R basis).
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from adaptive.post_close_tracker import (
    PostCloseTracker,
    classify_management_quality,
    classify_signal_quality,
    compute_excursions,
)

ENTRY_TS = datetime(2026, 6, 17, 10, 0, tzinfo=timezone.utc)


# ── Fakes ────────────────────────────────────────────────────────────────


class _Tick:
    def __init__(self, bid, ask):
        self.bid = bid
        self.ask = ask


class _FakeSource:
    """Duck-types platform_manager: get_price + fetch_market_data."""

    def __init__(self, bars: pd.DataFrame | None = None, tick: _Tick | None = None):
        self._bars = bars
        self._tick = tick
        self.price_calls = 0
        self.bar_calls = 0
        self.fail_price = False
        self.fail_bars = False

    def get_price(self, symbol):
        self.price_calls += 1
        if self.fail_price:
            raise RuntimeError("price feed down")
        return self._tick

    def fetch_market_data(self, symbol, timeframes, count):
        self.bar_calls += 1
        if self.fail_bars:
            raise RuntimeError("bar feed down")
        if self._bars is None:
            return {}
        return {"M1": self._bars}


class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now = self.now + timedelta(**kw)


def _bars(slope_high=0.1, slope_low=0.05, n=70, entry=100.0):
    rows = []
    for i in range(n):
        rows.append(
            {
                "time": ENTRY_TS + timedelta(minutes=i),
                "open": entry,
                "high": entry + i * slope_high,
                "low": entry - i * slope_low,
                "close": entry,
                "volume": 1,
            }
        )
    return pd.DataFrame(rows)


def _cfg(intervals=None, enabled=True, max_retries=3):
    class C:
        pass

    c = C()
    c.enabled = enabled
    c.check_intervals_minutes = intervals or [5, 15, 30, 60]
    c.max_retries = max_retries
    return c


# ── Pure math ──────────────────────────────────────────────────────────────


class TestExcursionMath:
    def test_long_mfe_mae(self):
        df = _bars()
        exc = compute_excursions(df, 100.0, "BUY", ENTRY_TS, ENTRY_TS + timedelta(minutes=60), 1.0)
        assert exc["mfe"] == pytest.approx(6.0)   # high at +60m = 106
        assert exc["mae"] == pytest.approx(3.0)   # low at +60m = 97
        assert exc["mfe_r"] == pytest.approx(6.0)
        assert exc["mae_r"] == pytest.approx(3.0)

    def test_short_flips_favorable_side(self):
        df = _bars()
        exc = compute_excursions(df, 100.0, "SELL", ENTRY_TS, ENTRY_TS + timedelta(minutes=60), 1.0)
        # For a short, favorable = price falling → MFE from the low side.
        assert exc["mfe"] == pytest.approx(3.0)
        assert exc["mae"] == pytest.approx(6.0)

    def test_window_limits_to_check_time(self):
        df = _bars()
        early = compute_excursions(df, 100.0, "BUY", ENTRY_TS, ENTRY_TS + timedelta(minutes=5), 1.0)
        late = compute_excursions(df, 100.0, "BUY", ENTRY_TS, ENTRY_TS + timedelta(minutes=60), 1.0)
        assert early["mfe"] < late["mfe"]  # MFE grows with the window

    def test_no_bars_in_window_returns_none(self):
        df = _bars()
        future = ENTRY_TS + timedelta(days=5)
        assert compute_excursions(df, 100.0, "BUY", future, future + timedelta(minutes=5), 1.0) is None

    def test_empty_or_malformed(self):
        assert compute_excursions(None, 100.0, "BUY", ENTRY_TS, ENTRY_TS, 1.0) is None
        assert compute_excursions(pd.DataFrame(), 100.0, "BUY", ENTRY_TS, ENTRY_TS, 1.0) is None
        bad = pd.DataFrame([{"time": ENTRY_TS, "close": 100}])
        assert compute_excursions(bad, 100.0, "BUY", ENTRY_TS, ENTRY_TS, 1.0) is None

    def test_zero_sl_distance_yields_none_r(self):
        df = _bars()
        exc = compute_excursions(df, 100.0, "BUY", ENTRY_TS, ENTRY_TS + timedelta(minutes=60), 0.0)
        assert exc["mfe"] == pytest.approx(6.0)
        assert exc["mfe_r"] is None and exc["mae_r"] is None


class TestClassification:
    def test_signal_quality_bands(self):
        assert classify_signal_quality(1.2) == "correct"
        assert classify_signal_quality(0.6) == "marginal"
        assert classify_signal_quality(0.2) == "wrong"
        assert classify_signal_quality(None) == "unknown"

    def test_loss_with_correct_signal_is_bad_management(self):
        assert classify_management_quality(-1.0, 2.0, True) == "bad_management"

    def test_loss_with_wrong_signal_is_bad_signal(self):
        assert classify_management_quality(-1.0, 0.2, False) == "bad_signal"

    def test_win_leaving_money_is_conservative(self):
        # captured 1R but price ran 3R → conservative
        assert classify_management_quality(1.0, 3.0, True) == "conservative_management"

    def test_win_capturing_move_is_good(self):
        assert classify_management_quality(2.0, 2.1, True) == "good_management"

    def test_unknown_when_missing(self):
        assert classify_management_quality(None, 1.0, True) == "unknown"


# ── Lifecycle ────────────────────────────────────────────────────────────────


def _tracker(tmp_path, clock, **cfg_kw):
    return PostCloseTracker(
        config=_cfg(**cfg_kw),
        db_path=str(tmp_path / "journal.db"),
        clock=clock,
    )


class TestLifecycle:
    def test_checks_not_due_do_not_run(self, tmp_path):
        clock = _Clock(ENTRY_TS)
        tk = _tracker(tmp_path, clock, intervals=[5, 15])
        tk.record_close(
            trade_id="t1", pair="EURUSD", direction="BUY",
            entry_price=100.0, exit_price=99.0, sl_price=99.0, tp_price=103.0,
            entry_timestamp=ENTRY_TS, exit_timestamp=ENTRY_TS,
        )
        src = _FakeSource(bars=_bars(), tick=_Tick(100.5, 100.6))
        # Only 2 minutes elapsed — nothing due.
        clock.advance(minutes=2)
        tk.process_pending_checks(src)
        assert tk.pending_count == 1
        assert src.price_calls == 0

    def test_full_completion_writes_final_row(self, tmp_path):
        clock = _Clock(ENTRY_TS)
        tk = _tracker(tmp_path, clock, intervals=[5, 15, 30, 60])
        # A LONG that LOST (exit below entry) but price ran our way → bad mgmt.
        tk.record_close(
            trade_id="t1", pair="EURUSD", direction="BUY",
            entry_price=100.0, exit_price=99.0, sl_price=99.0, tp_price=110.0,
            entry_timestamp=ENTRY_TS, exit_timestamp=ENTRY_TS + timedelta(minutes=2),
        )
        src = _FakeSource(bars=_bars(), tick=_Tick(105.9, 106.1))
        clock.advance(minutes=61)
        tk.process_pending_checks(src)
        assert tk.pending_count == 0  # finalised + removed
        acc = tk.get_signal_accuracy("EURUSD")
        assert acc == 1.0  # MFE far exceeded 1R
        mgmt = tk.get_management_score("EURUSD")
        assert mgmt["counts"].get("bad_management") == 1
        sl = tk.get_optimal_sl_stats("EURUSD")
        assert sl["sample_size"] == 1
        assert sl["median"] == pytest.approx(3.0)  # MAE survived

    def test_winner_capturing_move_is_good_management(self, tmp_path):
        clock = _Clock(ENTRY_TS)
        tk = _tracker(tmp_path, clock, intervals=[60])
        # Exit at 106 (≈ +6R), MFE 6R → captured the move.
        tk.record_close(
            trade_id="w1", pair="GBPUSD", direction="BUY",
            entry_price=100.0, exit_price=106.0, sl_price=99.0, tp_price=106.0,
            entry_timestamp=ENTRY_TS, exit_timestamp=ENTRY_TS + timedelta(minutes=60),
        )
        src = _FakeSource(bars=_bars(), tick=_Tick(105.9, 106.1))
        clock.advance(minutes=61)
        tk.process_pending_checks(src)
        mgmt = tk.get_management_score("GBPUSD")
        assert mgmt["counts"].get("good_management") == 1

    def test_partial_progress_keeps_pending(self, tmp_path):
        clock = _Clock(ENTRY_TS)
        tk = _tracker(tmp_path, clock, intervals=[5, 60])
        tk.record_close(
            trade_id="t1", pair="EURUSD", direction="BUY",
            entry_price=100.0, exit_price=101.0, sl_price=99.0, tp_price=103.0,
            entry_timestamp=ENTRY_TS, exit_timestamp=ENTRY_TS,
        )
        src = _FakeSource(bars=_bars(), tick=_Tick(100.5, 100.6))
        clock.advance(minutes=10)  # only the 5m check is due
        tk.process_pending_checks(src)
        assert tk.pending_count == 1  # 60m still outstanding


class TestPersistence:
    def test_pending_survives_restart(self, tmp_path):
        db = str(tmp_path / "journal.db")
        clock = _Clock(ENTRY_TS)
        tk = PostCloseTracker(config=_cfg(intervals=[5, 60]), db_path=db, clock=clock)
        tk.record_close(
            trade_id="t1", pair="EURUSD", direction="BUY",
            entry_price=100.0, exit_price=101.0, sl_price=99.0, tp_price=103.0,
            entry_timestamp=ENTRY_TS, exit_timestamp=ENTRY_TS,
        )
        # Simulate restart: brand-new instance reading the same DB.
        clock2 = _Clock(ENTRY_TS + timedelta(minutes=61))
        tk2 = PostCloseTracker(config=_cfg(intervals=[5, 60]), db_path=db, clock=clock2)
        assert tk2.pending_count == 1
        src = _FakeSource(bars=_bars(), tick=_Tick(105.9, 106.1))
        tk2.process_pending_checks(src)
        assert tk2.pending_count == 0
        assert tk2.get_signal_accuracy("EURUSD") == 1.0

    def test_finalised_rows_persist_for_accessors(self, tmp_path):
        db = str(tmp_path / "journal.db")
        clock = _Clock(ENTRY_TS)
        tk = PostCloseTracker(config=_cfg(intervals=[60]), db_path=db, clock=clock)
        tk.record_close(
            trade_id="t1", pair="EURUSD", direction="BUY",
            entry_price=100.0, exit_price=99.0, sl_price=99.0, tp_price=110.0,
            entry_timestamp=ENTRY_TS, exit_timestamp=ENTRY_TS,
        )
        clock.advance(minutes=61)
        src = _FakeSource(bars=_bars(), tick=_Tick(105.9, 106.1))
        tk.process_pending_checks(src)
        # Fresh reader sees the finalised data.
        tk2 = PostCloseTracker(config=_cfg(intervals=[60]), db_path=db, clock=_Clock(clock.now))
        assert tk2.get_signal_accuracy("EURUSD") == 1.0


class TestEdgeCases:
    def test_disabled_records_nothing(self, tmp_path):
        tk = _tracker(tmp_path, _Clock(ENTRY_TS), enabled=False)
        tk.record_close(
            trade_id="t1", pair="EURUSD", direction="BUY",
            entry_price=100.0, exit_price=99.0, sl_price=99.0, tp_price=103.0,
            entry_timestamp=ENTRY_TS, exit_timestamp=ENTRY_TS,
        )
        assert tk.pending_count == 0

    def test_empty_trade_id_skipped(self, tmp_path):
        tk = _tracker(tmp_path, _Clock(ENTRY_TS))
        tk.record_close(
            trade_id="", pair="EURUSD", direction="BUY",
            entry_price=100.0, exit_price=99.0, sl_price=99.0, tp_price=103.0,
            entry_timestamp=ENTRY_TS, exit_timestamp=ENTRY_TS,
        )
        assert tk.pending_count == 0

    def test_price_failure_retries_then_incomplete(self, tmp_path):
        clock = _Clock(ENTRY_TS)
        tk = _tracker(tmp_path, clock, intervals=[5], max_retries=3)
        tk.record_close(
            trade_id="t1", pair="OFFLINE", direction="BUY",
            entry_price=100.0, exit_price=99.0, sl_price=99.0, tp_price=103.0,
            entry_timestamp=ENTRY_TS, exit_timestamp=ENTRY_TS,
        )
        # Source where BOTH price and bars fail — nothing usable.
        src = _FakeSource(bars=None, tick=None)
        src.fail_price = True
        src.fail_bars = True
        clock.advance(minutes=6)
        # Each pass = 1 retry; after max_retries the check is marked incomplete.
        tk.process_pending_checks(src)
        assert tk.pending_count == 1
        tk.process_pending_checks(src)
        assert tk.pending_count == 1
        tk.process_pending_checks(src)
        # 3rd retry → incomplete → finalised + removed.
        assert tk.pending_count == 0
        # Incomplete trades are not graded.
        assert tk.get_optimal_sl_stats("OFFLINE")["sample_size"] == 0

    def test_recovers_when_data_returns_before_max_retries(self, tmp_path):
        clock = _Clock(ENTRY_TS)
        tk = _tracker(tmp_path, clock, intervals=[5], max_retries=3)
        tk.record_close(
            trade_id="t1", pair="EURUSD", direction="BUY",
            entry_price=100.0, exit_price=99.0, sl_price=99.0, tp_price=103.0,
            entry_timestamp=ENTRY_TS, exit_timestamp=ENTRY_TS,
        )
        src = _FakeSource(bars=_bars(), tick=_Tick(105.9, 106.1))
        src.fail_price = True
        src.fail_bars = True
        clock.advance(minutes=6)
        tk.process_pending_checks(src)  # retry 1
        assert tk.pending_count == 1
        src.fail_price = False
        src.fail_bars = False
        tk.process_pending_checks(src)  # now succeeds
        assert tk.pending_count == 0
        # Recovered with usable data → finalised with a real (non-incomplete) grade.
        assert tk.get_optimal_sl_stats("EURUSD")["sample_size"] == 1

    def test_very_short_trade_still_tracked(self, tmp_path):
        clock = _Clock(ENTRY_TS)
        tk = _tracker(tmp_path, clock, intervals=[5])
        tk.record_close(
            trade_id="t1", pair="EURUSD", direction="BUY",
            entry_price=100.0, exit_price=100.01, sl_price=99.0, tp_price=103.0,
            entry_timestamp=ENTRY_TS, exit_timestamp=ENTRY_TS + timedelta(seconds=3),
        )
        assert tk.pending_count == 1

    def test_missing_r_basis_does_not_crash(self, tmp_path):
        clock = _Clock(ENTRY_TS)
        tk = _tracker(tmp_path, clock, intervals=[60])
        # sl == entry → no R basis.
        tk.record_close(
            trade_id="t1", pair="EURUSD", direction="BUY",
            entry_price=100.0, exit_price=101.0, sl_price=100.0, tp_price=103.0,
            entry_timestamp=ENTRY_TS, exit_timestamp=ENTRY_TS,
        )
        clock.advance(minutes=61)
        src = _FakeSource(bars=_bars(), tick=_Tick(105.9, 106.1))
        tk.process_pending_checks(src)  # must not raise
        assert tk.pending_count == 0
        # signal_quality is "unknown" without an R basis → not counted as graded.
        assert tk.get_signal_accuracy("EURUSD") == 0.0

    def test_process_does_not_raise_on_bad_source(self, tmp_path):
        clock = _Clock(ENTRY_TS)
        tk = _tracker(tmp_path, clock, intervals=[5])
        tk.record_close(
            trade_id="t1", pair="EURUSD", direction="BUY",
            entry_price=100.0, exit_price=99.0, sl_price=99.0, tp_price=103.0,
            entry_timestamp=ENTRY_TS, exit_timestamp=ENTRY_TS,
        )
        clock.advance(minutes=6)

        class _Broken:
            def get_price(self, s):
                raise RuntimeError("boom")

            def fetch_market_data(self, *a):
                raise RuntimeError("boom")

        tk.process_pending_checks(_Broken())  # swallowed, still pending
        assert tk.pending_count == 1
