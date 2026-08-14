"""V-03 — the compressed direction+score exits are deferrable to the Brain.

Constitution §I/§IV/§V/§XVII: a re-derived scan direction+score must not be an
exit authority. ``WorkerConfig.scan_directional_exits_enabled`` (default True ⇒
behaviour unchanged) defers ONLY the three compressed-direction exits —
``invalidation`` (low-score / opposing), ``conviction_collapse`` (declining score
trajectory), and the structure-loss ``stall`` — to the AI Cognitive Brain. The
always-on hard-SL floor and the non-directional mechanics (TP / breakeven /
trailing / time-based stall clock) are unaffected.

Mirrors tests/test_execution_worker.py; pure stdlib (no numpy needed at author
time — CI exercises PositionWorker fully).
"""

from datetime import datetime, timezone

from execution.intents import IntentType
from execution.position_worker import PositionWorker, ScanContext, WorkerConfig
from execution.position_snapshot import PositionSnapshot

NOW = datetime(2025, 6, 15, 14, 0, tzinfo=timezone.utc)


def _snap(
    direction="BUY", entry_price=1.10000, sl=1.09900, sl_original=1.09900,
    tp1=1.11000, tp2=1.12000, current_price=1.10500, pnl_pips=50.0,
    pip_size=0.0001, pip_value_per_lot=10.0, lots=0.1, partial_closed=False,
    at_breakeven=False, tp1_hit=False, tp3=None, tp3_hit=False,
    trade_status="OPEN", open_time=None, entry_timeframe="M5", score=85,
    score_history=(), platform="mt5", symbol="EURUSD", order_id="T1",
    stake_usd=0.0, multiplier=100, broker_pnl=0.0, **kw,
):
    if open_time is None:
        open_time = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)  # long before NOW
    return PositionSnapshot(
        order_id=order_id, platform=platform, symbol=symbol, direction=direction,
        entry_price=entry_price, lots=lots, remaining_lots=lots, open_time=open_time,
        score=score, sl=sl, sl_original=sl_original, tp1=tp1, tp2=tp2,
        tp2_original=tp2, tp3=tp3, tp1_hit=tp1_hit, tp3_hit=tp3_hit,
        at_breakeven=at_breakeven, partial_closed=partial_closed,
        current_price=current_price, pnl_pips=pnl_pips, pip_size=pip_size,
        pip_value_per_lot=pip_value_per_lot, trade_status=trade_status,
        entry_timeframe=entry_timeframe, score_history=tuple(score_history),
        stake_usd=stake_usd, multiplier=multiplier, broker_pnl=broker_pnl,
        broker_lots=lots, candles_since_entry=0, highest_since_entry=current_price,
        lowest_since_entry=entry_price, trailing=False, re_entry_eligible=False,
        tm_trade_id="tm1", confluences=(), scale_in_count=0, **kw,
    )


def _sources(intents):
    return {i.source for i in intents}


# ── invalidation (low score / opposing) ──────────────────────────────────────

def test_invalidation_low_score_fires_by_default_and_defers_when_disabled():
    snap = _snap(pnl_pips=-5.0)
    scan = ScanContext(direction="SHORT", score=30)
    on = PositionWorker(WorkerConfig(invalidation_score_threshold=40)).evaluate(snap, NOW, scan=scan)
    assert "invalidation_low_score" in _sources(on)
    off = PositionWorker(WorkerConfig(
        invalidation_score_threshold=40, scan_directional_exits_enabled=False,
    )).evaluate(snap, NOW, scan=scan)
    assert "invalidation_low_score" not in _sources(off)


def test_invalidation_opposing_fires_by_default_and_defers_when_disabled():
    snap = _snap(direction="BUY", pnl_pips=-5.0)
    scan = ScanContext(direction="SHORT", score=80)
    on = PositionWorker(WorkerConfig(opposing_signal_threshold=75)).evaluate(snap, NOW, scan=scan)
    assert "invalidation_opposing" in _sources(on)
    off = PositionWorker(WorkerConfig(
        opposing_signal_threshold=75, scan_directional_exits_enabled=False,
    )).evaluate(snap, NOW, scan=scan)
    assert "invalidation_opposing" not in _sources(off)


# ── conviction collapse (declining score trajectory) ──────────────────────────

def test_conviction_collapse_fires_by_default_and_defers_when_disabled():
    snap = _snap(pnl_pips=5.0, score_history=(90, 86, 82, 78))
    scan = ScanContext(direction="LONG", score=78)
    base = dict(conviction_decline_cycles=4, conviction_decline_min_drop=3,
                conviction_profit_hold_pips=20.0)
    on = PositionWorker(WorkerConfig(**base)).evaluate(snap, NOW, scan=scan)
    assert "conviction_collapse" in _sources(on)
    off = PositionWorker(WorkerConfig(
        scan_directional_exits_enabled=False, **base,
    )).evaluate(snap, NOW, scan=scan)
    assert "conviction_collapse" not in _sources(off)


# ── structure-loss stall (reads scan direction/score) ─────────────────────────

def test_structure_loss_stall_defers_when_disabled():
    # Flat, aged position whose live scan opposes it ⇒ structure-loss stall.
    snap = _snap(direction="BUY", pnl_pips=0.5, current_price=1.10000)
    scan = ScanContext(direction="SHORT", score=10)
    base = dict(stall_requires_structure_loss=True, stall_min_hold_minutes=5.0)
    on = PositionWorker(WorkerConfig(**base)).evaluate(snap, NOW, scan=scan)
    assert "stall_exit" in _sources(on)
    off = PositionWorker(WorkerConfig(
        scan_directional_exits_enabled=False, **base,
    )).evaluate(snap, NOW, scan=scan)
    assert "stall_exit" not in _sources(off)


def test_time_based_stall_fallback_unaffected_when_disabled():
    # The non-directional clock stall (opt-out mode) must still fire when the
    # scan-directional exits are deferred — it is a time mechanic, not a vote.
    snap = _snap(direction="BUY", pnl_pips=0.5, current_price=1.10000)
    off = PositionWorker(WorkerConfig(
        stall_requires_structure_loss=False, scan_directional_exits_enabled=False,
    )).evaluate(snap, NOW, scan=ScanContext(direction="LONG", score=90))
    assert "stall_exit" in _sources(off)


# ── the hard-SL safety floor is never gated by the flag ───────────────────────

def test_hard_stop_loss_still_fires_when_scan_directional_disabled():
    snap = _snap(direction="BUY", sl=1.09900, current_price=1.09800)
    intents = PositionWorker(WorkerConfig(
        scan_directional_exits_enabled=False,
    )).evaluate(snap, NOW, scan=ScanContext(direction="LONG", score=90))
    assert any(i.intent_type == IntentType.CLOSE and "SL hit" in i.reason for i in intents)
