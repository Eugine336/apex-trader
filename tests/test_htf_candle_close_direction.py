"""Regression test for the HTF candle-close exit direction inversion.

Fresh entries store ``pos.direction`` as "LONG"/"SHORT", but the exit checks
compared it against the literal "BUY". For a LONG that made ``is_long`` False,
inverting the whole check: a BULLISH H1 close (which SUPPORTS a long) was treated
as opposing and closed the trade. The fix normalises the direction with
``pos.direction.upper() in ("BUY", "LONG")`` so the exit fires only when the
candle truly OPPOSES the trade.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

from platforms.trading_loop.exit_checks_mixin import ExitChecksMixin


def _h1_df(open_, close):
    """3-row H1 frame whose last CLOSED candle (index -2) has the given body."""
    hi = max(open_, close) + 0.0005
    lo = min(open_, close) - 0.0005
    idx = [
        datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 16, 13, 0, tzinfo=timezone.utc),  # <- index -2 (closed)
        datetime(2026, 6, 16, 14, 0, tzinfo=timezone.utc),  # <- index -1 (forming)
    ]
    rows = [
        {"open": 1.10, "high": 1.11, "low": 1.09, "close": 1.105},
        {"open": open_, "high": hi, "low": lo, "close": close},
        {"open": close, "high": close + 0.001, "low": close - 0.001, "close": close},
    ]
    return pd.DataFrame(rows, index=idx)


def _mixin(direction):
    m = ExitChecksMixin.__new__(ExitChecksMixin)
    m._position_last_h1_close = {}
    m.managed_positions = {}
    m._position_scores = {}
    m.position_store = MagicMock()
    m._record_closed_trade = MagicMock()
    m.trade_manager = MagicMock()
    m.trade_manager.get_trade.return_value = SimpleNamespace(pnl_pips=0.0)
    m.platforms = MagicMock()
    m.platforms.close_trade.return_value = SimpleNamespace(success=True, close_price=1.10)
    pos = SimpleNamespace(
        direction=direction, symbol="EURUSD", tm_trade_id="tm1", platform="mt5",
    )
    m.managed_positions["oid1"] = pos
    return m, pos


def _run(direction, candle):
    m, pos = _mixin(direction)
    bullish = _h1_df(1.1000, 1.1050)   # close > open
    bearish = _h1_df(1.1050, 1.1000)   # close < open
    df = bullish if candle == "bullish" else bearish
    m._check_htf_candle_close("oid1", pos, df, datetime.now(timezone.utc))
    return m.platforms.close_trade.called


def test_long_holds_on_bullish_close():
    # BULLISH candle supports a LONG → must NOT exit (the bug exited here).
    assert _run("LONG", "bullish") is False


def test_long_exits_on_bearish_close():
    assert _run("LONG", "bearish") is True


def test_short_holds_on_bearish_close():
    assert _run("SHORT", "bearish") is False


def test_short_exits_on_bullish_close():
    assert _run("SHORT", "bullish") is True


def test_buy_alias_still_holds_on_bullish():
    # Broker-recovered positions may store "BUY" — must behave like LONG.
    assert _run("BUY", "bullish") is False
    assert _run("SELL", "bullish") is True
