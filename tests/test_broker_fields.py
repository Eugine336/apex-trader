"""Characterization tests for the extracted broker-truth field readers (Phase K).

These pin the exact behaviour of the readers lifted out of
``event_driven_bootstrap.py`` into ``execution/broker_fields.py`` — the module is
loaded directly (bypassing the heavy ``execution`` package __init__) so the pure
readers are verifiable with no third-party dependencies.
"""

import importlib.util
import sys
import types
from pathlib import Path

# Stub loguru (absent offline) before importing the module under test.
if "loguru" not in sys.modules:
    _lg = types.ModuleType("loguru")
    _logger = types.SimpleNamespace(**{
        m: (lambda *a, **k: None)
        for m in ("debug", "info", "warning", "error", "critical", "success",
                  "trace", "exception")
    })
    _logger.bind = lambda *a, **k: _logger
    _logger.opt = lambda *a, **k: _logger
    _lg.logger = _logger
    sys.modules["loguru"] = _lg

_PATH = Path(__file__).resolve().parents[1] / "execution" / "broker_fields.py"
_spec = importlib.util.spec_from_file_location("_broker_fields_uut", _PATH)
bf = importlib.util.module_from_spec(_spec)
sys.modules["_broker_fields_uut"] = bf
_spec.loader.exec_module(bf)


class _Pos:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ── _broker_pnl ────────────────────────────────────────────────────────────────

def test_pnl_prefers_broker_field():
    assert bf._broker_pnl(_Pos(pnl=12.5, profit=99.0)) == 12.5


def test_pnl_falls_back_to_legacy_profit():
    assert bf._broker_pnl(_Pos(profit=-3.0)) == -3.0


def test_pnl_falls_back_to_broker_pnl_attr():
    assert bf._broker_pnl(_Pos(broker_pnl=4.0)) == 4.0


def test_pnl_zero_is_preserved_not_treated_as_missing():
    # pnl uses "is None" checks, so a real 0.0 must NOT fall through to profit.
    assert bf._broker_pnl(_Pos(pnl=0.0, profit=50.0)) == 0.0


def test_pnl_default_zero_when_absent():
    assert bf._broker_pnl(_Pos()) == 0.0


# ── _broker_entry_price ─────────────────────────────────────────────────────────

def test_entry_price_prefers_open_price():
    assert bf._broker_entry_price(_Pos(open_price=1.2345, entry_price=9.9)) == 1.2345


def test_entry_price_falls_back_to_legacy():
    assert bf._broker_entry_price(_Pos(entry_price=1.5)) == 1.5


def test_entry_price_uses_default_when_absent_or_zero():
    assert bf._broker_entry_price(_Pos(), default=2.0) == 2.0
    # open_price=0 is falsy → legacy → default
    assert bf._broker_entry_price(_Pos(open_price=0.0), default=3.0) == 3.0


# ── _broker_tp ──────────────────────────────────────────────────────────────────

def test_tp_prefers_broker_tp():
    assert bf._broker_tp(_Pos(tp=1.10, tp1=9.9)) == 1.10


def test_tp_falls_back_to_tp1():
    assert bf._broker_tp(_Pos(tp1=1.25)) == 1.25


def test_tp_zero_when_absent():
    assert bf._broker_tp(_Pos()) == 0.0


# ── _broker_pip_value_from_spec ─────────────────────────────────────────────────

def _spec_d(tick_value, tick_size):
    return {"trade_tick_value": tick_value, "trade_tick_size": tick_size}


def test_pip_value_derived_from_spec():
    # 0.5 * (0.0001 / 0.0001) = 0.5
    assert bf._broker_pip_value_from_spec(_spec_d(0.5, 0.0001), 0.0001, 1.0) == 0.5


def test_pip_value_scales_with_pip_and_tick_size():
    # 1.0 * (0.1 / 0.01) = 10.0
    assert bf._broker_pip_value_from_spec(_spec_d(1.0, 0.01), 0.1, 99.0) == 10.0


def test_pip_value_fallback_on_empty_spec():
    assert bf._broker_pip_value_from_spec({}, 0.0001, 0.0001) == 0.0001


def test_pip_value_fallback_on_missing_tick_size():
    assert bf._broker_pip_value_from_spec({"trade_tick_value": 1.0}, 0.0001, 7.0) == 7.0


def test_pip_value_fallback_on_zero_tick_size():
    assert bf._broker_pip_value_from_spec(_spec_d(1.0, 0.0), 0.0001, 3.0) == 3.0


def test_pip_value_fallback_when_derivation_nonpositive():
    # tick_value 0 → derived pv not > 0 → fallback
    assert bf._broker_pip_value_from_spec(_spec_d(0.0, 0.0001), 0.0001, 5.0) == 5.0
