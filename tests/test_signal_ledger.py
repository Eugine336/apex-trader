"""
Tests for the universal Signal Ledger (adaptive/signal_ledger.py).

Dependency-light — sqlite3 + stdlib only, no torch/pandas/numpy. Verifies
signal recording, NEUTRAL rejection, gate-block + trade linkage, price grading
in both directions, MFE/MAE accumulation, the traded-vs-blocked accuracy split,
SQLite persistence across reopen, and exception safety on bad data.
"""

import time

import pytest

from adaptive.signal_ledger import (
    SignalLedger,
    SignalRecord,
    SignalOutcome,
    _signed_move_pct,
    new_signal_id,
)


@pytest.fixture
def ledger(tmp_path):
    led = SignalLedger(
        db_path=tmp_path / "sig.db",
        grading_delay_minutes=0.0,   # finalise immediately on first observation
        check_intervals=[5, 15, 30],
        min_move_pct=0.1,
    )
    yield led
    led.close()


def _sig(pair="EURUSD", emitter="momentum", direction="LONG", price=100.0, **kw):
    return SignalRecord(
        pair=pair, emitter=emitter, direction=direction,
        strength=kw.pop("strength", 0.8), price_at_signal=price,
        context=kw.pop("context", {"timeframe": "M5"}), **kw,
    )


class TestRecording:
    def test_record_and_fetch(self, ledger):
        sid = ledger.record_signal(_sig())
        assert sid and sid.startswith("sig_")
        rec = ledger.get_signal(sid)
        assert rec is not None
        assert rec["pair"] == "EURUSD"
        assert rec["emitter"] == "momentum"
        assert rec["direction"] == "LONG"
        assert rec["context"] == {"timeframe": "M5"}
        assert rec["outcome"]["graded"] is False

    def test_neutral_is_ignored(self, ledger):
        assert ledger.record_signal(_sig(direction="NEUTRAL")) is None
        assert ledger.record_signal(_sig(direction="")) is None

    def test_signal_id_and_timestamp_autofill(self):
        r = SignalRecord(pair="X", emitter="m", direction="LONG")
        assert r.signal_id.startswith("sig_")
        assert r.timestamp > 0

    def test_explicit_signal_id_preserved(self, ledger):
        r = _sig()
        r.signal_id = "sig_custom123"
        sid = ledger.record_signal(r)
        assert sid == "sig_custom123"

    def test_new_signal_id_helper(self):
        assert new_signal_id().startswith("sig_")
        assert new_signal_id() != new_signal_id()


class TestGrading:
    def test_correct_long_direction(self, ledger):
        sid = ledger.record_signal(_sig(direction="LONG", price=100.0))
        out = ledger.run_grading_cycle({"EURUSD": 100.5})
        assert out == {"observed": 1, "graded": 1}
        rec = ledger.get_signal(sid)
        assert rec["outcome"]["direction_correct"] is True

    def test_wrong_long_direction(self, ledger):
        sid = ledger.record_signal(_sig(direction="LONG", price=100.0))
        ledger.run_grading_cycle({"EURUSD": 99.5})
        rec = ledger.get_signal(sid)
        assert rec["outcome"]["direction_correct"] is False

    def test_correct_short_direction(self, ledger):
        sid = ledger.record_signal(_sig(direction="SHORT", price=100.0))
        ledger.run_grading_cycle({"EURUSD": 99.0})
        rec = ledger.get_signal(sid)
        assert rec["outcome"]["direction_correct"] is True

    def test_wrong_short_direction(self, ledger):
        sid = ledger.record_signal(_sig(direction="SHORT", price=100.0))
        ledger.run_grading_cycle({"EURUSD": 101.0})
        rec = ledger.get_signal(sid)
        assert rec["outcome"]["direction_correct"] is False

    def test_below_min_move_is_not_correct(self, ledger):
        # 0.05% move < 0.1% min threshold → not correct.
        sid = ledger.record_signal(_sig(direction="LONG", price=100.0))
        ledger.run_grading_cycle({"EURUSD": 100.05})
        rec = ledger.get_signal(sid)
        assert rec["outcome"]["direction_correct"] is False

    def test_grade_signal_single(self, ledger):
        sid = ledger.record_signal(_sig(direction="LONG", price=100.0))
        assert ledger.grade_signal(sid, {"EURUSD": 100.5}) is True
        # Already graded → no re-grade.
        assert ledger.grade_signal(sid, {"EURUSD": 100.5}) is False

    def test_missing_price_skips(self, ledger):
        ledger.record_signal(_sig())
        out = ledger.run_grading_cycle({})  # no price for the pair
        assert out == {"observed": 0, "graded": 0}

    def test_mfe_mae_accumulate(self, tmp_path):
        # Long-delay ledger so grading stays open across multiple observations.
        led = SignalLedger(db_path=tmp_path / "m.db", grading_delay_minutes=999.0)
        sid = led.record_signal(_sig(direction="LONG", price=100.0))
        led.run_grading_cycle({"EURUSD": 102.0})   # +2% favorable
        led.run_grading_cycle({"EURUSD": 98.0})    # -2% adverse
        rec = led.get_signal(sid)
        assert rec["outcome"]["graded"] is False    # still open
        assert rec["outcome"]["max_favorable_move_pct"] == pytest.approx(2.0, abs=1e-6)
        assert rec["outcome"]["max_adverse_move_pct"] == pytest.approx(2.0, abs=1e-6)
        led.close()

    def test_check_delays_stamped(self, tmp_path):
        led = SignalLedger(
            db_path=tmp_path / "d.db",
            grading_delay_minutes=999.0,
            check_intervals=[5, 15],
        )
        r = _sig(direction="LONG", price=100.0)
        r.timestamp = time.time() - 20 * 60   # 20 minutes ago
        sid = led.record_signal(r)
        led.run_grading_cycle({"EURUSD": 101.0})
        rec = led.get_signal(sid)
        delays = rec["outcome"]["check_delays"]
        assert "5" in delays and "15" in delays
        led.close()


class TestGateAndTradeLinkage:
    def test_record_gate_block(self, ledger):
        sid = ledger.record_signal(_sig())
        assert ledger.record_gate_block(sid, "planner_skip") is True
        rec = ledger.get_signal(sid)
        assert rec["gate_blocked_by"] == "planner_skip"

    def test_record_trade_opened_clears_block(self, ledger):
        sid = ledger.record_signal(_sig())
        ledger.record_gate_block(sid, "planner_skip")
        assert ledger.record_trade_opened(sid, "order_1") is True
        rec = ledger.get_signal(sid)
        assert rec["trade_opened"] is True
        assert rec["trade_id"] == "order_1"
        assert rec["gate_blocked_by"] is None

    def test_gate_block_for_pair(self, ledger):
        ledger.record_signal(_sig(pair="EURUSD", emitter="momentum"))
        ledger.record_signal(_sig(pair="EURUSD", emitter="structure"))
        ledger.record_signal(_sig(pair="GBPUSD", emitter="momentum"))
        n = ledger.record_gate_block_for_pair("EURUSD", "max_trades")
        assert n == 2
        gbp = ledger.get_graded_signals(pair="GBPUSD", include_ungraded=True)
        assert gbp[0]["gate_blocked_by"] is None

    def test_trade_opened_for_pair_direction_filter(self, ledger):
        ledger.record_signal(_sig(pair="EURUSD", emitter="momentum", direction="LONG"))
        ledger.record_signal(_sig(pair="EURUSD", emitter="vwap", direction="SHORT"))
        n = ledger.record_trade_opened_for_pair("EURUSD", "order_9", direction="LONG")
        assert n == 1
        rows = ledger.get_graded_signals(pair="EURUSD", include_ungraded=True)
        longs = [r for r in rows if r["direction"] == "LONG"]
        shorts = [r for r in rows if r["direction"] == "SHORT"]
        assert longs[0]["trade_opened"] is True
        assert shorts[0]["trade_opened"] is False


class TestAccuracySeparation:
    def test_traded_vs_blocked_split(self, ledger):
        # Two LONG signals; one becomes a trade (correct), one blocked (wrong).
        s_trade = ledger.record_signal(_sig(pair="EURUSD", direction="LONG", price=100.0))
        s_block = ledger.record_signal(_sig(pair="GBPUSD", direction="LONG", price=100.0))
        ledger.record_trade_opened(s_trade, "ord_t")
        ledger.record_gate_block(s_block, "entry_score")
        ledger.run_grading_cycle({"EURUSD": 101.0, "GBPUSD": 99.0})

        acc = ledger.get_emitter_accuracy("momentum")
        assert acc["total"] == 2
        assert acc["traded"] == 1
        assert acc["blocked"] == 1
        assert acc["accuracy_traded"] == 1.0
        assert acc["accuracy_blocked"] == 0.0
        assert acc["accuracy"] == 0.5

    def test_accuracy_all_emitters(self, ledger):
        ledger.record_signal(_sig(emitter="momentum", direction="LONG", price=100.0))
        ledger.record_signal(_sig(emitter="structure", direction="LONG", price=100.0))
        ledger.run_grading_cycle({"EURUSD": 101.0})
        allacc = ledger.get_emitter_accuracy_all()
        assert set(allacc.keys()) == {"momentum", "structure"}
        assert allacc["momentum"]["accuracy"] == 1.0

    def test_pair_filter(self, ledger):
        ledger.record_signal(_sig(pair="EURUSD", direction="LONG", price=100.0))
        ledger.record_signal(_sig(pair="GBPUSD", direction="LONG", price=100.0))
        ledger.run_grading_cycle({"EURUSD": 101.0, "GBPUSD": 99.0})
        acc = ledger.get_emitter_accuracy("momentum", pair="EURUSD")
        assert acc["total"] == 1
        assert acc["accuracy"] == 1.0

    def test_lookback_limits_rows(self, ledger):
        for i in range(5):
            r = _sig(direction="LONG", price=100.0)
            r.timestamp = time.time() - (5 - i)  # distinct past ordering
            ledger.record_signal(r)
        ledger.run_grading_cycle({"EURUSD": 101.0})
        acc = ledger.get_emitter_accuracy("momentum", lookback_trades=3)
        assert acc["total"] == 3


class TestTradeOutcomeMerge:
    def test_provider_merged_on_finalise(self, tmp_path):
        led = SignalLedger(
            db_path=tmp_path / "to.db",
            grading_delay_minutes=0.0,
            trade_outcome_provider=lambda tid: {"pnl_r": 2.1, "exit_cause": "tp"},
        )
        sid = led.record_signal(_sig(direction="LONG", price=100.0))
        led.record_trade_opened(sid, "ord_x")
        led.run_grading_cycle({"EURUSD": 101.0})
        rec = led.get_signal(sid)
        assert rec["outcome"]["trade_outcome"] == {"pnl_r": 2.1, "exit_cause": "tp"}
        led.close()

    def test_attach_trade_outcome(self, ledger):
        sid = ledger.record_signal(_sig(direction="LONG", price=100.0))
        ledger.record_trade_opened(sid, "ord_y")
        n = ledger.attach_trade_outcome("ord_y", {"pnl_r": -1.0})
        assert n == 1
        rec = ledger.get_signal(sid)
        assert rec["outcome"]["trade_outcome"] == {"pnl_r": -1.0}


class TestPersistence:
    def test_survives_reopen(self, tmp_path):
        db = tmp_path / "persist.db"
        led = SignalLedger(db_path=db, grading_delay_minutes=0.0)
        sid = led.record_signal(_sig(direction="LONG", price=100.0))
        led.run_grading_cycle({"EURUSD": 101.0})
        led.close()

        led2 = SignalLedger(db_path=db, grading_delay_minutes=0.0)
        rec = led2.get_signal(sid)
        assert rec is not None
        assert rec["outcome"]["graded"] is True
        assert rec["outcome"]["direction_correct"] is True
        acc = led2.get_emitter_accuracy("momentum")
        assert acc["total"] == 1
        led2.close()


class TestExceptionSafety:
    def test_record_none_safe(self, ledger):
        assert ledger.record_signal(None) is None

    def test_unknown_signal_lookups_safe(self, ledger):
        assert ledger.get_signal("nope") is None
        assert ledger.record_gate_block("nope", "g") is True  # UPDATE no-op, no crash
        assert ledger.record_trade_opened("nope", "t") is True

    def test_grading_with_bad_price_safe(self, ledger):
        sid = ledger.record_signal(_sig(price=100.0))
        # zero/negative current price is ignored, not graded
        out = ledger.run_grading_cycle({"EURUSD": 0.0})
        assert out["graded"] == 0
        assert sid is not None

    def test_zero_reference_price_safe(self, ledger):
        sid = ledger.record_signal(_sig(price=0.0))
        out = ledger.run_grading_cycle({"EURUSD": 100.0})
        # reference price 0 → signed move 0 → graded but not correct
        rec = ledger.get_signal(sid)
        assert rec["outcome"]["direction_correct"] is False
        assert out["graded"] == 1

    def test_empty_accuracy_safe(self, ledger):
        acc = ledger.get_emitter_accuracy("does_not_exist")
        assert acc["total"] == 0
        assert acc["accuracy"] == 0.0


class TestHelpers:
    def test_signed_move_long(self):
        assert _signed_move_pct("LONG", 100.0, 101.0) == pytest.approx(1.0)
        assert _signed_move_pct("LONG", 100.0, 99.0) == pytest.approx(-1.0)

    def test_signed_move_short(self):
        assert _signed_move_pct("SHORT", 100.0, 99.0) == pytest.approx(1.0)
        assert _signed_move_pct("SHORT", 100.0, 101.0) == pytest.approx(-1.0)

    def test_signed_move_bad_ref(self):
        assert _signed_move_pct("LONG", 0.0, 100.0) == 0.0

    def test_signal_outcome_dataclass(self):
        o = SignalOutcome(signal_id="s1", price_at_signal=100.0)
        assert o.signal_id == "s1"
        assert o.direction_correct is False
        assert o.check_delays == {}
