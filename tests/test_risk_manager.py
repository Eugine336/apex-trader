"""
Tests for the Risk Management Layer (adaptive/risk_manager.py, L8) and its
TunerAgent adapter (adaptive.tunable_adapters.RiskManagerTunable).

Covers the drawdown circuit breaker (daily halt, rolling cooldown + restore,
hard stop + flatten signal), correlated / directional / per-pair / per-regime
exposure caps, the structured RiskGate denial + risk-event logging, the
cold-start and disabled no-ops (everything passes, sizing factor 1.0), SQLite
persistence round-trips, the dashboard snapshot shape, and Tunable protocol
compliance.

Deterministic: drives fixed equity / position scenarios against temp SQLite DBs
so nothing touches the real data/ dir.
"""

import os
import tempfile

import pytest

from config import RiskManagementConfig
from adaptive.risk_manager import (
    RiskManager,
    RiskDecision,
    RISK_NORMAL,
    RISK_COOLDOWN,
    RISK_HALTED_DAILY,
    RISK_HALTED_HARD,
)
from adaptive.tunable import Tunable, TuneContext, TuneFrequency
from adaptive.tunable_adapters import RiskManagerTunable


# ── Fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    for p in (path, path + "-wal", path + "-shm"):
        try:
            os.remove(p)
        except OSError:
            pass


def _mgr(tmp_db, **kw):
    params = dict(db_path=tmp_db, enabled=True)
    params.update(kw)
    return RiskManager(**params)


# ── Drawdown circuit breaker ────────────────────────────────────────────────


def test_daily_drawdown_halts(tmp_db):
    mgr = _mgr(tmp_db, daily_drawdown_limit_pct=3.0)
    mgr.on_trade_closed(0.0, 10000.0)      # establishes the day-start equity
    mgr.on_trade_closed(-400.0, 9600.0)    # 4% daily drawdown
    assert mgr.state == RISK_HALTED_DAILY
    decision = mgr.can_open_position(
        "EURUSD", "BUY", open_positions=[], account_balance=9600.0,
    )
    assert decision.allowed is False
    assert decision.rule == "daily_halt"
    mgr.close()


def test_rolling_drawdown_cooldown_then_restore(tmp_db):
    # daily high so only the rolling limit trips; cooldown elapses immediately.
    mgr = _mgr(
        tmp_db, daily_drawdown_limit_pct=2.0, rolling_drawdown_limit_pct=8.0,
        hard_stop_drawdown_pct=15.0, cooldown_hours=0.0,
    )
    # New UTC-day each close would reset daily; keep them same-day by not
    # crossing midnight (real wall clock within one test run).
    mgr._day_start_equity = 0.0  # let first close anchor the day
    mgr.on_trade_closed(0.0, 10000.0)
    # Disable the daily halt path for this test by lifting the daily anchor.
    mgr._day_start_equity = 9150.0  # daily DD < 2% from this anchor
    mgr.on_trade_closed(-700.0, 9100.0)   # rolling DD 9% from the 10k peak
    assert mgr.state == RISK_COOLDOWN
    assert mgr.sizing_factor() < 1.0
    # Trades still allowed during cooldown (sizing handles de-risking).
    assert mgr.can_open_position("EURUSD", "BUY", open_positions=[]).allowed
    # Recovery close with elapsed cooldown restores NORMAL.
    mgr._day_start_equity = 9600.0
    mgr.on_trade_closed(600.0, 9700.0)    # rolling DD 3% < 8%, cooldown elapsed
    assert mgr.state == RISK_NORMAL
    assert mgr.sizing_factor() == 1.0
    mgr.close()


def test_hard_stop_blocks_and_flattens(tmp_db):
    mgr = _mgr(tmp_db, daily_drawdown_limit_pct=20.0, rolling_drawdown_limit_pct=10.0,
               hard_stop_drawdown_pct=15.0)
    mgr.on_trade_closed(0.0, 10000.0)
    mgr._day_start_equity = 8500.0  # neutralise daily so the hard stop is isolated
    mgr.on_trade_closed(-1600.0, 8400.0)  # rolling DD 16% >= 15%
    assert mgr.state == RISK_HALTED_HARD
    assert mgr.should_flatten() is True
    assert mgr.can_open_position("EURUSD", "BUY", open_positions=[]).rule == "hard_stop"
    mgr.close()


# ── Exposure caps ───────────────────────────────────────────────────────────


def test_max_simultaneous_positions(tmp_db):
    mgr = _mgr(tmp_db, max_simultaneous_positions=2)
    positions = [{"pair": "EURUSD", "direction": "BUY"}, {"pair": "GBPUSD", "direction": "BUY"}]
    decision = mgr.can_open_position("USDJPY", "BUY", open_positions=positions)
    assert decision.allowed is False
    assert decision.rule == "max_positions"
    mgr.close()


def test_max_per_pair_positions(tmp_db):
    mgr = _mgr(tmp_db, max_per_pair_positions=1)
    positions = [{"pair": "EURUSD", "direction": "BUY"}]
    decision = mgr.can_open_position("EURUSD", "BUY", open_positions=positions)
    assert decision.allowed is False
    assert decision.rule == "max_per_pair"
    mgr.close()


def test_directional_exposure_cap(tmp_db):
    mgr = _mgr(tmp_db, max_directional_exposure_pct=60.0, max_per_pair_positions=5)
    positions = [{"pair": "EURUSD", "direction": "BUY"}, {"pair": "GBPUSD", "direction": "BUY"}]
    decision = mgr.can_open_position("USDJPY", "BUY", open_positions=positions)
    assert decision.allowed is False
    assert decision.rule == "directional_exposure"
    mgr.close()


def test_regime_concentration_cap(tmp_db):
    # High directional cap so only the regime concentration trips.
    mgr = _mgr(tmp_db, max_directional_exposure_pct=95.0, max_per_pair_positions=5,
               max_per_regime_pct=40.0)
    positions = [
        {"pair": "EURUSD", "direction": "BUY", "regime": "TRENDING_UP"},
        {"pair": "GBPUSD", "direction": "SELL", "regime": "TRENDING_UP"},
    ]
    decision = mgr.can_open_position(
        "USDJPY", "BUY", open_positions=positions, regime="TRENDING_UP",
    )
    assert decision.allowed is False
    assert decision.rule == "regime_concentration"
    mgr.close()


# ── Correlation ─────────────────────────────────────────────────────────────


def test_correlated_exposure_blocked(tmp_db):
    mgr = _mgr(tmp_db, correlation_threshold=0.7, max_correlated_exposure_factor=1.0,
               correlation_update_interval=1, max_per_pair_positions=5)
    mgr.update_correlations({
        "EURUSD": [1.0, 2.0, 3.0, 4.0, 5.0],
        "GBPUSD": [2.0, 4.0, 6.0, 8.0, 10.0],  # perfectly correlated returns
    })
    assert mgr.correlation("EURUSD", "GBPUSD") > 0.7
    positions = [{"pair": "EURUSD", "direction": "BUY"}]
    decision = mgr.can_open_position("GBPUSD", "BUY", open_positions=positions)
    assert decision.allowed is False
    assert decision.rule == "correlated_exposure"
    mgr.close()


def test_opposite_direction_correlated_allowed(tmp_db):
    # A correlated pair in the OPPOSITE direction is a hedge, not concentration:
    # the correlated-exposure check only counts same-direction peers.
    mgr = _mgr(tmp_db, correlation_threshold=0.7, max_correlated_exposure_factor=1.0,
               correlation_update_interval=1, max_per_pair_positions=5,
               max_directional_exposure_pct=95.0)
    mgr.update_correlations({
        "EURUSD": [1.0, 2.0, 3.0, 4.0, 5.0],
        "GBPUSD": [2.0, 4.0, 6.0, 8.0, 10.0],  # perfectly correlated returns
    })
    positions = [{"pair": "EURUSD", "direction": "BUY"}]
    decision = mgr.can_open_position("GBPUSD", "SELL", open_positions=positions)
    assert decision.allowed is True
    mgr.close()


# ── Structured denial + logging ─────────────────────────────────────────────


def test_decision_is_structured(tmp_db):
    mgr = _mgr(tmp_db, max_simultaneous_positions=1)
    decision = mgr.can_open_position(
        "EURUSD", "BUY", open_positions=[{"pair": "GBPUSD", "direction": "BUY"}],
    )
    assert isinstance(decision, RiskDecision)
    allowed, reason = decision.as_tuple()
    assert allowed is False and reason
    mgr.close()


def test_blocked_trade_is_logged(tmp_db):
    mgr = _mgr(tmp_db, max_simultaneous_positions=1)
    mgr.can_open_position(
        "EURUSD", "BUY", open_positions=[{"pair": "GBPUSD", "direction": "BUY"}],
    )
    events = mgr.get_state()["risk_events"]
    assert len(events) >= 1
    assert events[0]["rule"] == "max_positions"
    mgr.close()


# ── Cold start / disabled ───────────────────────────────────────────────────


def test_cold_start_allows_all(tmp_db):
    mgr = _mgr(tmp_db)
    decision = mgr.can_open_position("EURUSD", "BUY", open_positions=[])
    assert decision.allowed is True
    assert mgr.sizing_factor() == 1.0
    assert mgr.should_flatten() is False
    mgr.close()


def test_disabled_is_noop(tmp_db):
    mgr = _mgr(tmp_db, enabled=False)
    mgr.on_trade_closed(-5000.0, 5000.0)   # huge loss must NOT trip a disabled breaker
    big_book = [{"pair": f"P{i}", "direction": "BUY"} for i in range(50)]
    decision = mgr.can_open_position("EURUSD", "BUY", open_positions=big_book)
    assert decision.allowed is True
    assert mgr.sizing_factor() == 1.0
    assert mgr.state == RISK_NORMAL
    mgr.close()


# ── Persistence ─────────────────────────────────────────────────────────────


def test_persistence_roundtrip(tmp_db):
    mgr = _mgr(tmp_db)
    mgr.on_trade_closed(0.0, 10000.0)
    mgr.on_trade_closed(-500.0, 9500.0)
    dd_before = mgr.rolling_drawdown_pct()
    mgr.close()
    reopened = _mgr(tmp_db)
    assert abs(reopened.rolling_drawdown_pct() - dd_before) < 1e-6
    assert abs(reopened._peak_equity - 10000.0) < 1e-6
    reopened.close()


# ── Dashboard snapshot ──────────────────────────────────────────────────────


def test_dashboard_state_shape(tmp_db):
    mgr = _mgr(tmp_db)
    mgr.on_trade_closed(0.0, 10000.0)
    state = mgr.get_state()
    for key in ("enabled", "state", "rolling_drawdown_pct", "daily_drawdown_pct",
                "sizing_factor", "should_flatten", "limits", "risk_events",
                "equity_curve", "correlations"):
        assert key in state
    assert state["state"] == RISK_NORMAL
    mgr.close()


# ── Tunable adapter ─────────────────────────────────────────────────────────


def test_tunable_protocol_compliance(tmp_db):
    mgr = _mgr(tmp_db)
    adapter = RiskManagerTunable(mgr)
    assert isinstance(adapter, Tunable)
    assert adapter.tunable_name == "risk_manager"
    assert adapter.frequency == TuneFrequency.ON_DEMAND
    params = adapter.get_current_params()
    assert "rolling_drawdown_limit_pct" in params
    ok, _ = adapter.validate_params(params)
    assert ok is True
    res = adapter.tune(TuneContext(force=True))
    assert res.success and res.skipped
    mgr.close()


def test_tunable_rejects_inverted_limits(tmp_db):
    mgr = _mgr(tmp_db)
    adapter = RiskManagerTunable(mgr)
    ok, _reason = adapter.validate_params({
        "daily_drawdown_limit_pct": 10.0,
        "rolling_drawdown_limit_pct": 5.0,
    })
    assert ok is False
    ok, _reason = adapter.validate_params({"cooldown_sizing_factor": 1.5})
    assert ok is False
    mgr.close()


# ── Config validation ───────────────────────────────────────────────────────


def test_config_defaults_active():
    cfg = RiskManagementConfig()
    assert cfg.enabled is True


def test_config_enforces_limit_ordering():
    with pytest.raises(ValueError):
        RiskManagementConfig(
            daily_drawdown_limit_pct=10.0,
            rolling_drawdown_limit_pct=5.0,
            hard_stop_drawdown_pct=15.0,
        )
    with pytest.raises(ValueError):
        RiskManagementConfig(cooldown_sizing_factor=2.0)
