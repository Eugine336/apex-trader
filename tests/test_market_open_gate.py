"""Market-open gate — fresh-tick SESSION signal on top of broker trade_mode.

The cognition loop skips origination reasoning (and the advisory council) for a
symbol whose market is closed. ``trade_mode`` stays "full" straight through a
weekend, so the gate also consults tick freshness: a symbol with no fresh tick
(weekend/holiday) is treated as CLOSED — which is what stops the Brain wasting
LLM calls analysing closed instruments.
"""

import types

from event_driven_bootstrap import EventDrivenSystem, _MARKET_CLOSED_TICK_AGE_SECONDS
import threading

import event_driven_bootstrap as edb


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


def _bind(conn):
    """A minimal object with the three market-open methods bound, so internal
    self.* dispatch works without constructing a full EventDrivenSystem."""
    ns = types.SimpleNamespace(
        _pm=types.SimpleNamespace(get_connector=lambda s: conn),
        _market_open_state={}, _market_open_log_lock=threading.Lock(),
    )
    for name in ("_check_market_open", "_market_open_status", "_log_market_transition"):
        setattr(ns, name, types.MethodType(getattr(edb.EventDrivenSystem, name), ns))
    return ns


def _gate(conn, symbol="EURUSD"):
    return _bind(conn)._check_market_open(symbol)


class _SpyLogger:
    def __init__(self):
        self.infos = []

    def info(self, msg, *a, **k):
        try:
            self.infos.append(str(msg).format(*a))
        except Exception:
            self.infos.append(str(msg))

    def __getattr__(self, _name):
        def _noop(*a, **k):
            return None
        return _noop


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


# ── Edge-triggered logging: closed once, then silence; reopen once ───────────

def test_closed_logs_once_then_silences(monkeypatch):
    spy = _SpyLogger()
    monkeypatch.setattr(edb, "logger", spy)
    self_ = _bind(_Conn(trade_mode=4, fresh=False))
    for _ in range(4):   # four cycles, market stays closed
        assert self_._check_market_open("EURUSD") is False
    closed = [m for m in spy.infos if "EURUSD closed" in m]
    assert len(closed) == 1   # logged once, then silent


def test_reopen_logs_once(monkeypatch):
    spy = _SpyLogger()
    monkeypatch.setattr(edb, "logger", spy)
    state = {"fresh": False}
    conn = types.SimpleNamespace(
        get_symbol_spec=lambda s: {"trade_mode": 4},
        has_fresh_tick=lambda s, age=None: state["fresh"],
    )
    self_ = _bind(conn)
    self_._check_market_open("EURUSD")   # closed → log
    self_._check_market_open("EURUSD")   # closed → silent
    state["fresh"] = True
    self_._check_market_open("EURUSD")   # reopened → log
    self_._check_market_open("EURUSD")   # open → silent
    assert len([m for m in spy.infos if "EURUSD closed" in m]) == 1
    assert len([m for m in spy.infos if "EURUSD reopened" in m]) == 1


def test_open_symbol_not_logged(monkeypatch):
    # An always-open / active symbol produces NO market-open log line (its own
    # reasoning logs show it's active).
    spy = _SpyLogger()
    monkeypatch.setattr(edb, "logger", spy)
    self_ = _bind(_Conn(trade_mode=4, fresh=True))
    for _ in range(3):
        assert self_._check_market_open("EURUSD") is True
    assert [m for m in spy.infos if "market-open" in m] == []


# ── Entry admission: unknown market/broker state blocks new exposure ─────────


def _bind_entry(conn):
    ns = _bind(conn)
    for name in ("_entry_market_open", "_is_broker_available"):
        setattr(ns, name, types.MethodType(getattr(edb.EventDrivenSystem, name), ns))
    return ns


def test_entry_gate_blocks_when_market_state_is_unknown():
    # No freshness probe and a spec without trade_mode => no positive evidence.
    class _Blind:
        def get_symbol_spec(self, symbol):
            return {}

    assert _bind_entry(_Blind())._entry_market_open("EURUSD") is False
    # Observation-side gate keeps its historical behaviour on unknown state.
    assert _bind_entry(_Blind())._check_market_open("EURUSD") is True


def test_entry_gate_opens_only_with_positive_evidence():
    assert _bind_entry(_Conn(trade_mode=4, fresh=True))._entry_market_open("EURUSD") is True
    assert _bind_entry(_Conn(trade_mode=4, fresh=None))._entry_market_open("EURUSD") is True  # trade_mode known
    assert _bind_entry(_Conn(trade_mode=4, fresh=False))._entry_market_open("EURUSD") is False
    assert _bind_entry(_Conn(trade_mode=0, fresh=True))._entry_market_open("EURUSD") is False


def test_entry_gate_blocks_without_connector():
    ns = _bind_entry(None)
    ns._pm = types.SimpleNamespace(get_connector=lambda s: (_ for _ in ()).throw(KeyError(s)))
    assert ns._entry_market_open("EURUSD") is False
    assert ns._is_broker_available("EURUSD") is False


def test_broker_available_requires_positive_connection_report():
    class _Up:
        def is_connected(self):
            return True

    class _Down:
        def is_connected(self):
            return False

    class _NoProbe:
        pass

    class _Raises:
        def is_connected(self):
            raise RuntimeError("socket")

    assert _bind_entry(_Up())._is_broker_available("EURUSD") is True
    assert _bind_entry(_Down())._is_broker_available("EURUSD") is False
    assert _bind_entry(_NoProbe())._is_broker_available("EURUSD") is False
    assert _bind_entry(_Raises())._is_broker_available("EURUSD") is False


def test_event_driven_pip_size_prefers_broker_spec_then_static_table():
    def _pip(pm):
        ns = types.SimpleNamespace(_pm=pm, _note_pip_size_fallback=lambda *a, **k: None)
        return types.MethodType(edb.EventDrivenSystem._safe_pip_size, ns)("EURUSD")

    # Broker spec wins when it reports a positive pip size.
    assert _pip(types.SimpleNamespace(get_symbol_spec=lambda s: {"pip_size": 0.5})) == 0.5
    # Empty/invalid spec or a raising manager falls back to the static table.
    static = _pip(types.SimpleNamespace())
    assert _pip(types.SimpleNamespace(get_symbol_spec=lambda s: {})) == static
    assert _pip(types.SimpleNamespace(get_symbol_spec=lambda s: {"pip_size": 0})) == static

    def _boom(_s):
        raise RuntimeError("down")

    assert _pip(types.SimpleNamespace(get_symbol_spec=_boom)) == static
