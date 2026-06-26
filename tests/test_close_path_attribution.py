"""Regression tests for the trade-close attribution fixes.

Covers two audit bugs in the close path (``EventDrivenSystem._on_trade_closed``):

* BUG 1 — ``regime`` and ``session`` were hardcoded to "" on the ``TradeRecord``
  that feeds RegimeLearner / SessionLearner / EVEstimator, blinding the entire
  learning pipeline. They must now be populated from the entry-context info
  dict (``regime_at_entry`` / ``session_at_entry``), and ``score`` /
  ``confluences`` likewise (were 0 / "").

* BUG 2 — the PostCloseTracker received the *close* time as ``entry_timestamp``,
  so forward MFE/MAE checks were scheduled from the wrong moment. The caller now
  passes the entry-context ISO ``entry_time``; this verifies the tracker honours
  it (forward checks fire relative to entry, not close).

The close path itself is deeply wired, so these tests exercise the exact field
transformation the fix performs (info dict → TradeRecord / record_close args)
rather than standing up the full EventDrivenSystem.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from brain.trade_journal import TradeRecord, TradeJournal


# The exact transformation the close path applies (mirrors
# event_driven_bootstrap.py TradeRecord construction).
def _record_from_info(info: dict, *, symbol="EURUSD", direction="LONG") -> TradeRecord:
    return TradeRecord(
        pair=symbol,
        direction=direction,
        entry=float(info.get("entry_price", 0.0) or 0.0),
        exit=1.1050,
        pnl=12.0,
        score=int(info.get("score", 0) or 0),
        confluences=list(info.get("confluences", []) or []),
        regime=str(info.get("regime_at_entry", "") or ""),
        session=str(info.get("session_at_entry", "") or ""),
        spread=0.0,
        slippage=0.0,
        entry_type="event_driven",
        time_to_tp1=0.0,
        time_to_exit=0.0,
        outcome="win",
        pnl_dollars=10.0,
        regime_at_entry=str(info.get("regime_at_entry", "") or ""),
    )


def test_regime_session_score_confluences_populated_from_info():
    info = {
        "entry_price": 1.1000,
        "regime_at_entry": "TRENDING",
        "session_at_entry": "LONDON",
        "score": 87,
        "confluences": ["structure", "fvg", "momentum"],
    }
    rec = _record_from_info(info)
    assert rec.regime == "TRENDING"
    assert rec.session == "LONDON"
    assert rec.score == 87
    assert rec.confluences == ["structure", "fvg", "momentum"]


def test_missing_info_degrades_to_empty_not_crash():
    rec = _record_from_info({})
    assert rec.regime == ""
    assert rec.session == ""
    assert rec.score == 0
    assert rec.confluences == []


def test_journal_round_trips_regime_session(tmp_path):
    db = str(tmp_path / "tj.db")
    journal = TradeJournal(db_path=db)
    info = {
        "entry_price": 1.2000,
        "regime_at_entry": "RANGING",
        "session_at_entry": "NEW_YORK",
        "score": 73,
        "confluences": ["order_block", "liquidity"],
    }
    rec = _record_from_info(info, symbol="GBPUSD", direction="SHORT")

    async def _run():
        await journal.log_trade(rec)
        return await journal.get_all_trades_as_dicts()

    rows = asyncio.run(_run())
    assert len(rows) == 1
    row = rows[0]
    assert row["regime"] == "RANGING"
    assert row["session"] == "NEW_YORK"
    assert row["score"] == 73
    assert row["confluences_raw"] == ["order_block", "liquidity"]


def test_post_close_tracker_uses_entry_time_not_close_time(tmp_path):
    """Forward checks must be scheduled from entry time, not close time."""
    from adaptive.post_close_tracker import PostCloseTracker

    class _Cfg:
        post_close_tracking_enabled = True
        post_close_max_retries = 3
        check_intervals_minutes = [5, 15]

    class _Clock:
        def __init__(self, now):
            self.now = now

        def __call__(self):
            return self.now

    entry_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Trade closed 90 minutes after entry.
    close_ts = entry_ts + timedelta(minutes=90)
    clock = _Clock(close_ts)

    tk = PostCloseTracker(config=_Cfg(), db_path=str(tmp_path / "pc.db"), clock=clock)

    # Caller passes the entry-context ISO entry_time (the fix), NOT close time.
    tk.record_close(
        trade_id="T1",
        pair="EURUSD",
        direction="LONG",
        entry_price=1.1000,
        exit_price=1.1050,
        entry_timestamp=entry_ts.isoformat(),
        exit_timestamp=close_ts.isoformat(),
        entry_score=80,
        entry_confluences=["structure"],
    )

    pending = tk._pending.get("T1")
    assert pending is not None
    # The scheduled window must anchor on entry_ts. If the close time had been
    # passed (the bug), entry_timestamp would equal close_ts and the +5/+15m
    # checks would already be ~90m stale.
    assert pending.entry_timestamp == entry_ts
    assert pending.entry_timestamp != close_ts
    assert pending.entry_score == 80
    assert pending.entry_confluences == ["structure"]
