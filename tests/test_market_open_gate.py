"""Market-open gate — fresh-tick SESSION signal on top of broker trade_mode.

The cognition loop skips origination reasoning (and the advisory council) for a
symbol whose market is closed. ``trade_mode`` stays "full" straight through a
weekend, so the gate also consults tick freshness: a symbol with no fresh tick
(weekend/holiday) is treated as CLOSED — which is what stops the Brain wasting
LLM calls analysing closed instruments.
"""

import types

from event_driven_bootstrap import EventDrivenSystem, _MARKET_CLOSED_TICK_AGE_SECONDS


class _Conn:
    """Connector exposing both signals (MT5-like)."""

    def __init__(self, trade_mode=4, fresh=None):
        self._trade_mode = trade_mode
        self._fresh = fresh

    def get_symbol_spec(self, symbol):
        return {"trade_mode": self._trade_mode}

    def has_fresh_tick(self, symbol, max_age_seconds=None):
        return self._fresh


class _ConnNoProbe:
    """Older connector without the freshness probe (Deriv / legacy)."""

    def __init__(self, trade_mode=4):
        self._trade_mode = trade_mode

    def get_symbol_spec(self, symbol):
        return {"trade_mode": self._trade_mode}


def _gate(conn, symbol="EURUSD"):
    pm = types.SimpleNamespace(get_connector=lambda s: conn)
    self_ = types.SimpleNamespace(_pm=pm)
    return EventDrivenSystem._check_market_open(self_, symbol)


def test_threshold_is_conservative():
    # Far larger than the ~120s trading staleness limit, so a quiet-but-open
    # market is never mis-flagged closed.
    assert _MARKET_CLOSED_TICK_AGE_SECONDS >= 300.0


def test_open_when_fresh_tick_and_trade_mode_full():
    assert _gate(_Conn(trade_mode=4, fresh=True)) is True


def test_closed_on_weekend_stale_tick_despite_trade_mode_full():
    # THE bug: trade_mode stays 4 (full) on a weekend but there is no fresh tick.
    assert _gate(_Conn(trade_mode=4, fresh=False)) is False


def test_closed_on_trade_mode_disabled():
    assert _gate(_Conn(trade_mode=0, fresh=True)) is False


def test_closed_on_trade_mode_closeonly():
    assert _gate(_Conn(trade_mode=3, fresh=True)) is False


def test_fail_open_when_freshness_unknown():
    assert _gate(_Conn(trade_mode=4, fresh=None)) is True


def test_backward_compatible_connector_without_probe():
    # A connector that doesn't expose has_fresh_tick behaves exactly as before.
    assert _gate(_ConnNoProbe(trade_mode=4)) is True


# ── MT5 has_fresh_tick math (reuses get_price's staleness computation) ───────

def _fake_tick(age_seconds):
    import time as _t
    return types.SimpleNamespace(time=_t.time() - age_seconds, bid=1.1, ask=1.1)


def _mt5_self():
    return types.SimpleNamespace(_max_tick_age_seconds=120.0, symbol_map=lambda s: s)


def test_mt5_has_fresh_tick_true_when_recent(monkeypatch):
    from platforms.mt5 import mt5_connector as m
    monkeypatch.setattr(m.mt5, "symbol_info_tick", lambda s: _fake_tick(5))
    assert m.MT5Connector.has_fresh_tick(_mt5_self(), "EURUSD", 900.0) is True


def test_mt5_has_fresh_tick_false_when_weekend_stale(monkeypatch):
    from platforms.mt5 import mt5_connector as m
    # ~12h old — the exact "Stale tick for EURUSD: 42956.2s old" weekend case.
    monkeypatch.setattr(m.mt5, "symbol_info_tick", lambda s: _fake_tick(42956))
    assert m.MT5Connector.has_fresh_tick(_mt5_self(), "EURUSD", 900.0) is False


def test_mt5_has_fresh_tick_none_when_no_tick(monkeypatch):
    from platforms.mt5 import mt5_connector as m
    monkeypatch.setattr(m.mt5, "symbol_info_tick", lambda s: None)
    assert m.MT5Connector.has_fresh_tick(_mt5_self(), "EURUSD", 900.0) is None


def test_mt5_has_fresh_tick_none_when_unmapped():
    from platforms.mt5 import mt5_connector as m
    self_ = types.SimpleNamespace(_max_tick_age_seconds=120.0, symbol_map=lambda s: None)
    assert m.MT5Connector.has_fresh_tick(self_, "EURUSD", 900.0) is None
