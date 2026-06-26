"""Tests for execution/intent_aggregator.py — IntentAggregator Phase 5."""

import threading
from datetime import datetime, timedelta, timezone


from execution.intent_aggregator import (
    AggregatorConfig,
    IntentAggregator,
    should_skip_sl_update,
)
from execution.intents import Intent, IntentType


# ── Helpers ───────────────────────────────────────────────────────────

TS = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
TS1 = TS + timedelta(milliseconds=10)
TS2 = TS + timedelta(milliseconds=20)
TS3 = TS + timedelta(milliseconds=30)


def _close(ticket="T1", symbol="EURUSD", source="sl", reason="hit", ts=TS):
    return Intent.close(symbol=symbol, ticket=ticket, source=source, reason=reason, timestamp=ts)


def _sl(ticket="T1", symbol="EURUSD", new_sl=1.100, source="trail", reason="trail", ts=TS):
    return Intent.modify_sl(
        symbol=symbol, ticket=ticket, new_sl=new_sl, source=source, reason=reason, timestamp=ts,
    )


def _tp(ticket="T1", symbol="EURUSD", new_tp=1.120, source="adjust", reason="adj", ts=TS):
    return Intent.modify_tp(
        symbol=symbol, ticket=ticket, new_tp=new_tp, source=source, reason=reason, timestamp=ts,
    )


def _partial(ticket="T1", symbol="EURUSD", fraction=0.5, source="tp1", reason="tp1 hit", ts=TS):
    return Intent.partial_close(
        symbol=symbol, ticket=ticket, fraction=fraction, source=source, reason=reason, timestamp=ts,
    )


# ── Empty flush ──────────────────────────────────────────────────────

class TestEmptyFlush:
    def test_flush_empty_returns_empty(self):
        agg = IntentAggregator()
        assert agg.flush() == []

    def test_flush_after_flush_returns_empty(self):
        agg = IntentAggregator()
        agg.submit([_close()])
        agg.flush()
        assert agg.flush() == []


# ── Single intent passthrough ────────────────────────────────────────

class TestPassthrough:
    def test_single_close(self):
        agg = IntentAggregator()
        agg.submit([_close()])
        result = agg.flush()
        assert len(result) == 1
        assert result[0].intent_type == IntentType.CLOSE

    def test_single_modify_sl(self):
        agg = IntentAggregator()
        agg.submit([_sl()])
        result = agg.flush()
        assert len(result) == 1
        assert result[0].intent_type == IntentType.MODIFY_SL

    def test_single_modify_tp(self):
        agg = IntentAggregator()
        agg.submit([_tp()])
        result = agg.flush()
        assert len(result) == 1
        assert result[0].intent_type == IntentType.MODIFY_TP

    def test_single_partial_close(self):
        agg = IntentAggregator()
        agg.submit([_partial()])
        result = agg.flush()
        assert len(result) == 1
        assert result[0].intent_type == IntentType.PARTIAL_CLOSE


# ── CLOSE-beats-all ──────────────────────────────────────────────────

class TestCloseBeatsAll:
    def test_close_discards_modify_sl(self):
        agg = IntentAggregator()
        agg.submit([_sl(ticket="T1"), _close(ticket="T1")])
        result = agg.flush()
        assert len(result) == 1
        assert result[0].intent_type == IntentType.CLOSE

    def test_close_discards_modify_tp(self):
        agg = IntentAggregator()
        agg.submit([_tp(ticket="T1"), _close(ticket="T1")])
        result = agg.flush()
        assert len(result) == 1
        assert result[0].intent_type == IntentType.CLOSE

    def test_close_discards_partial_close(self):
        agg = IntentAggregator()
        agg.submit([_partial(ticket="T1"), _close(ticket="T1")])
        result = agg.flush()
        assert len(result) == 1
        assert result[0].intent_type == IntentType.CLOSE

    def test_close_discards_all_types_combined(self):
        agg = IntentAggregator()
        agg.submit([
            _sl(ticket="T1", ts=TS),
            _tp(ticket="T1", ts=TS1),
            _partial(ticket="T1", ts=TS2),
            _close(ticket="T1", ts=TS3),
        ])
        result = agg.flush()
        assert len(result) == 1
        assert result[0].intent_type == IntentType.CLOSE

    def test_multiple_closes_keeps_earliest(self):
        agg = IntentAggregator()
        agg.submit([
            _close(ticket="T1", source="a", ts=TS2),
            _close(ticket="T1", source="b", ts=TS),
            _close(ticket="T1", source="c", ts=TS1),
        ])
        result = agg.flush()
        assert len(result) == 1
        assert result[0].source == "b"
        assert result[0].timestamp == TS

    def test_close_on_one_position_does_not_affect_another(self):
        agg = IntentAggregator()
        agg.submit([
            _close(ticket="T1"),
            _sl(ticket="T2", new_sl=1.10),
        ])
        result = agg.flush()
        assert len(result) == 2
        types = {r.position_ticket: r.intent_type for r in result}
        assert types["T1"] == IntentType.CLOSE
        assert types["T2"] == IntentType.MODIFY_SL


# ── Tightest SL wins ────────────────────────────────────────────────

class TestTightestSL:
    def test_long_position_highest_sl_wins(self):
        agg = IntentAggregator()
        agg.register_position("T1", "LONG", current_sl=1.090, pip_size=0.0001)
        agg.submit([
            _sl(ticket="T1", new_sl=1.095, source="dynamic", ts=TS),
            _sl(ticket="T1", new_sl=1.098, source="breakeven", ts=TS1),
            _sl(ticket="T1", new_sl=1.093, source="spread", ts=TS2),
        ])
        result = agg.flush()
        assert len(result) == 1
        assert result[0].new_sl == 1.098
        assert result[0].source == "breakeven"

    def test_short_position_lowest_sl_wins(self):
        agg = IntentAggregator()
        agg.register_position("T1", "SHORT", current_sl=1.110, pip_size=0.0001)
        agg.submit([
            _sl(ticket="T1", new_sl=1.105, source="dynamic", ts=TS),
            _sl(ticket="T1", new_sl=1.102, source="breakeven", ts=TS1),
            _sl(ticket="T1", new_sl=1.107, source="spread", ts=TS2),
        ])
        result = agg.flush()
        assert len(result) == 1
        assert result[0].new_sl == 1.102
        assert result[0].source == "breakeven"

    def test_buy_direction_treated_as_long(self):
        agg = IntentAggregator()
        agg.register_position("T1", "BUY", current_sl=1.090, pip_size=0.0001)
        agg.submit([
            _sl(ticket="T1", new_sl=1.095, ts=TS),
            _sl(ticket="T1", new_sl=1.098, ts=TS1),
        ])
        result = agg.flush()
        assert result[0].new_sl == 1.098

    def test_sell_direction_treated_as_short(self):
        agg = IntentAggregator()
        agg.register_position("T1", "SELL", current_sl=1.110, pip_size=0.0001)
        agg.submit([
            _sl(ticket="T1", new_sl=1.105, ts=TS),
            _sl(ticket="T1", new_sl=1.102, ts=TS1),
        ])
        result = agg.flush()
        assert result[0].new_sl == 1.102

    def test_no_direction_falls_back_to_latest_timestamp(self):
        agg = IntentAggregator()
        agg.submit([
            _sl(ticket="T1", new_sl=1.095, source="early", ts=TS),
            _sl(ticket="T1", new_sl=1.098, source="late", ts=TS1),
        ])
        result = agg.flush()
        assert len(result) == 1
        assert result[0].source == "late"

    def test_single_sl_passes_through(self):
        agg = IntentAggregator()
        agg.register_position("T1", "LONG", current_sl=1.090, pip_size=0.0001)
        agg.submit([_sl(ticket="T1", new_sl=1.098)])
        result = agg.flush()
        assert len(result) == 1
        assert result[0].new_sl == 1.098


# ── SL dedup ─────────────────────────────────────────────────────────

class TestSLDedup:
    def test_skip_sl_within_threshold(self):
        agg = IntentAggregator()
        agg.register_position("T1", "LONG", current_sl=1.09900, pip_size=0.0001)
        agg.submit([_sl(ticket="T1", new_sl=1.09903)])
        result = agg.flush()
        assert len(result) == 0

    def test_keep_sl_outside_threshold(self):
        agg = IntentAggregator()
        agg.register_position("T1", "LONG", current_sl=1.09900, pip_size=0.0001)
        agg.submit([_sl(ticket="T1", new_sl=1.09960)])
        result = agg.flush()
        assert len(result) == 1

    def test_custom_threshold(self):
        cfg = AggregatorConfig(sl_dedup_threshold_pips=1.0)
        agg = IntentAggregator(cfg)
        agg.register_position("T1", "LONG", current_sl=1.09900, pip_size=0.0001)
        agg.submit([_sl(ticket="T1", new_sl=1.09907)])
        assert len(agg.flush()) == 0

        agg.register_position("T1", "LONG", current_sl=1.09900, pip_size=0.0001)
        agg.submit([_sl(ticket="T1", new_sl=1.09920)])
        assert len(agg.flush()) == 1

    def test_no_position_info_skips_dedup(self):
        agg = IntentAggregator()
        agg.submit([_sl(ticket="T1", new_sl=1.09903)])
        result = agg.flush()
        assert len(result) == 1

    def test_zero_current_sl_skips_dedup(self):
        agg = IntentAggregator()
        agg.register_position("T1", "LONG", current_sl=0.0, pip_size=0.0001)
        agg.submit([_sl(ticket="T1", new_sl=1.09903)])
        assert len(agg.flush()) == 1

    def test_zero_pip_size_skips_dedup(self):
        agg = IntentAggregator()
        agg.register_position("T1", "LONG", current_sl=1.099, pip_size=0.0)
        agg.submit([_sl(ticket="T1", new_sl=1.09903)])
        assert len(agg.flush()) == 1

    def test_dedup_on_tightest_sl_result(self):
        """Tightest SL is picked first, THEN dedup is applied."""
        agg = IntentAggregator()
        agg.register_position("T1", "LONG", current_sl=1.09900, pip_size=0.0001)
        agg.submit([
            _sl(ticket="T1", new_sl=1.09902, ts=TS),
            _sl(ticket="T1", new_sl=1.09904, ts=TS1),
        ])
        result = agg.flush()
        assert len(result) == 0


# ── Latest TP wins ───────────────────────────────────────────────────

class TestLatestTP:
    def test_latest_tp_wins(self):
        agg = IntentAggregator()
        agg.submit([
            _tp(ticket="T1", new_tp=1.120, source="early", ts=TS),
            _tp(ticket="T1", new_tp=1.125, source="late", ts=TS1),
        ])
        result = agg.flush()
        assert len(result) == 1
        assert result[0].new_tp == 1.125
        assert result[0].source == "late"

    def test_single_tp_passes(self):
        agg = IntentAggregator()
        agg.submit([_tp(ticket="T1")])
        result = agg.flush()
        assert len(result) == 1


# ── PARTIAL_CLOSE stacking ──────────────────────────────────────────

class TestPartialCloseStacking:
    def test_partials_under_cap_kept_individually(self):
        agg = IntentAggregator()
        agg.submit([
            _partial(ticket="T1", fraction=0.3, source="tp1"),
            _partial(ticket="T1", fraction=0.2, source="tp3"),
        ])
        result = agg.flush()
        assert len(result) == 2
        fractions = [r.close_fraction for r in result]
        assert 0.3 in fractions
        assert 0.2 in fractions

    def test_partials_at_cap_become_close(self):
        agg = IntentAggregator()
        agg.submit([
            _partial(ticket="T1", fraction=0.5, source="tp1"),
            _partial(ticket="T1", fraction=0.5, source="tp3"),
        ])
        result = agg.flush()
        assert len(result) == 1
        assert result[0].intent_type == IntentType.CLOSE
        assert "tp1" in result[0].reason
        assert "tp3" in result[0].reason

    def test_partials_over_cap_become_close(self):
        agg = IntentAggregator()
        agg.submit([
            _partial(ticket="T1", fraction=0.6, source="tp1"),
            _partial(ticket="T1", fraction=0.6, source="tp3"),
        ])
        result = agg.flush()
        assert len(result) == 1
        assert result[0].intent_type == IntentType.CLOSE

    def test_custom_cap(self):
        cfg = AggregatorConfig(max_partial_close_fraction=0.8)
        agg = IntentAggregator(cfg)
        agg.submit([
            _partial(ticket="T1", fraction=0.5),
            _partial(ticket="T1", fraction=0.4),
        ])
        result = agg.flush()
        assert len(result) == 1
        assert result[0].intent_type == IntentType.CLOSE


# ── Priority sorting ────────────────────────────────────────────────

class TestPrioritySorting:
    def test_close_sorted_before_modify(self):
        agg = IntentAggregator()
        agg.submit([
            _sl(ticket="T1"),
            _close(ticket="T2"),
            _tp(ticket="T3"),
        ])
        result = agg.flush()
        assert result[0].intent_type == IntentType.CLOSE
        assert result[0].position_ticket == "T2"

    def test_priority_order_close_partial_sl_tp(self):
        agg = IntentAggregator()
        agg.submit([
            _tp(ticket="T4"),
            _sl(ticket="T3"),
            _partial(ticket="T2"),
            _close(ticket="T1"),
        ])
        result = agg.flush()
        types = [r.intent_type for r in result]
        assert types == [
            IntentType.CLOSE,
            IntentType.PARTIAL_CLOSE,
            IntentType.MODIFY_SL,
            IntentType.MODIFY_TP,
        ]


# ── Multi-position independence ─────────────────────────────────────

class TestMultiPosition:
    def test_independent_positions_not_merged(self):
        agg = IntentAggregator()
        agg.submit([
            _sl(ticket="T1", new_sl=1.095),
            _sl(ticket="T2", new_sl=1.200),
            _close(ticket="T3"),
        ])
        result = agg.flush()
        assert len(result) == 3
        tickets = {r.position_ticket for r in result}
        assert tickets == {"T1", "T2", "T3"}

    def test_close_on_t1_keeps_sl_on_t2(self):
        agg = IntentAggregator()
        agg.submit([
            _close(ticket="T1"),
            _sl(ticket="T1", new_sl=1.095),
            _sl(ticket="T2", new_sl=1.200),
        ])
        result = agg.flush()
        t1 = [r for r in result if r.position_ticket == "T1"]
        t2 = [r for r in result if r.position_ticket == "T2"]
        assert len(t1) == 1 and t1[0].intent_type == IntentType.CLOSE
        assert len(t2) == 1 and t2[0].intent_type == IntentType.MODIFY_SL


# ── Mixed intents for one position ──────────────────────────────────

class TestMixedIntents:
    def test_sl_and_tp_for_same_position(self):
        agg = IntentAggregator()
        agg.register_position("T1", "LONG", current_sl=1.090, pip_size=0.0001)
        agg.submit([
            _sl(ticket="T1", new_sl=1.098),
            _tp(ticket="T1", new_tp=1.120),
        ])
        result = agg.flush()
        assert len(result) == 2
        types = {r.intent_type for r in result}
        assert IntentType.MODIFY_SL in types
        assert IntentType.MODIFY_TP in types

    def test_sl_tp_partial_for_same_position(self):
        agg = IntentAggregator()
        agg.register_position("T1", "LONG", current_sl=1.090, pip_size=0.0001)
        agg.submit([
            _sl(ticket="T1", new_sl=1.098),
            _tp(ticket="T1", new_tp=1.120),
            _partial(ticket="T1", fraction=0.3),
        ])
        result = agg.flush()
        assert len(result) == 3


# ── Thread safety ────────────────────────────────────────────────────

class TestThreadSafety:
    def test_concurrent_submits(self):
        agg = IntentAggregator()
        n_threads = 8
        intents_per_thread = 50
        barrier = threading.Barrier(n_threads)

        def worker(tid):
            barrier.wait()
            for i in range(intents_per_thread):
                agg.submit([
                    _sl(
                        ticket=f"T{tid}-{i}",
                        symbol="EURUSD",
                        new_sl=1.09 + i * 0.001,
                    ),
                ])

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        result = agg.flush()
        assert len(result) == n_threads * intents_per_thread

    def test_concurrent_submit_and_register(self):
        agg = IntentAggregator()
        barrier = threading.Barrier(2)

        def submitter():
            barrier.wait()
            for i in range(100):
                agg.submit([_sl(ticket=f"T{i}", new_sl=1.1 + i * 0.001)])

        def registerer():
            barrier.wait()
            for i in range(100):
                agg.register_position(f"T{i}", "LONG", 1.09, 0.0001)

        t1 = threading.Thread(target=submitter)
        t2 = threading.Thread(target=registerer)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        result = agg.flush()
        assert len(result) == 100


# ── should_skip_sl_update standalone helper ──────────────────────────

class TestShouldSkipSLUpdate:
    def test_within_threshold_returns_true(self):
        assert should_skip_sl_update(1.09900, 1.09903, pip_size=0.0001) is True

    def test_outside_threshold_returns_false(self):
        assert should_skip_sl_update(1.09900, 1.09960, pip_size=0.0001) is False

    def test_exact_threshold_returns_false(self):
        assert should_skip_sl_update(1.09900, 1.09950, pip_size=0.0001) is False

    def test_custom_threshold(self):
        assert should_skip_sl_update(1.09900, 1.09909, pip_size=0.0001, threshold_pips=1.0) is True

    def test_zero_pip_size_returns_false(self):
        assert should_skip_sl_update(1.099, 1.099, pip_size=0.0) is False

    def test_negative_pip_size_returns_false(self):
        assert should_skip_sl_update(1.099, 1.099, pip_size=-0.0001) is False

    def test_gold_pip_size(self):
        assert should_skip_sl_update(2650.00, 2650.03, pip_size=0.01, threshold_pips=0.5) is False
        assert should_skip_sl_update(2650.00, 2650.003, pip_size=0.01, threshold_pips=0.5) is True


# ── Edge cases ───────────────────────────────────────────────────────

class TestEdgeCases:
    def test_submit_empty_list(self):
        agg = IntentAggregator()
        agg.submit([])
        assert agg.flush() == []

    def test_multiple_submits_before_flush(self):
        agg = IntentAggregator()
        agg.submit([_sl(ticket="T1")])
        agg.submit([_sl(ticket="T2")])
        agg.submit([_close(ticket="T3")])
        result = agg.flush()
        assert len(result) == 3

    def test_flush_clears_position_info(self):
        agg = IntentAggregator()
        agg.register_position("T1", "LONG", 1.099, 0.0001)
        agg.submit([_sl(ticket="T1", new_sl=1.09903)])
        agg.flush()

        agg.submit([_sl(ticket="T1", new_sl=1.09903)])
        result = agg.flush()
        assert len(result) == 1

    def test_partial_close_aggregated_to_close_has_correct_source(self):
        agg = IntentAggregator()
        agg.submit([
            _partial(ticket="T1", fraction=0.6, source="tp1"),
            _partial(ticket="T1", fraction=0.6, source="scale_out"),
        ])
        result = agg.flush()
        assert len(result) == 1
        assert result[0].intent_type == IntentType.CLOSE
        assert result[0].source == "aggregated_partial_close"

    def test_different_symbols_same_ticket_handled(self):
        agg = IntentAggregator()
        agg.submit([
            _sl(ticket="T1", symbol="EURUSD", new_sl=1.10),
            _sl(ticket="T1", symbol="EURUSD", new_sl=1.11),
        ])
        result = agg.flush()
        assert len(result) == 1
