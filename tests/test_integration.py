"""Grand integration tests for APEX TRADER (P2).

End-to-end exercises that drive the REAL adaptive stack — not mocks — through
the backtest harness and the ops lifecycle layer:

* The L7 :class:`~adaptive.regime_detector.RegimeDetector` and L8
  :class:`~adaptive.risk_manager.RiskManager` are driven by
  :class:`~backtest.runner.BacktestRunner` exactly as production wires them
  (the runner replaces only the broker, never the trading logic).
* Adaptive layers the runner does not wire (signal ledger, behaviour discovery)
  are fed real trade data directly to prove they record + stay dormant cold.
* The ops layer (shutdown flush, crash-marker recovery, health snapshot, the
  process watchdog, structured logging) is exercised against a lightweight loop
  stand-in plus the real components.

Every test is self-contained, uses temp paths (no production DB pollution), and
runs in well under 30s. Silent failure is treated as a bug — these tests assert
that no layer throws and that blocked trades are recorded, not swallowed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger

from backtest import synthetic_data as sd
from backtest.runner import BacktestRunner
from config import OpsConfig
from ops.lifecycle import HealthCheck, ShutdownManager, StartupRecovery
from ops.logging_config import audit_risk, audit_trade, configure_structured_logging


@pytest.fixture(autouse=True)
def _ensure_real_deps(monkeypatch, request):
    """Guard against the suite-wide numpy/loguru stub pollution.

    Several pre-existing test modules irreversibly replace
    ``sys.modules['numpy']`` with a bare stub and ``loguru.logger`` with a
    ``MagicMock`` at import time (no teardown). That breaks any later test that
    relies on real numpy (backtest metrics, ``pytest.approx``) or a real loguru
    logger (the structured-logging file sinks become no-ops). This fixture
    restores the real implementations for the duration of each test, then
    reverts everything — including putting the mock logger back — so the
    polluting / mock-asserting tests around us are unaffected.
    """
    import importlib
    import sys

    # ── numpy ──────────────────────────────────────────────────────────────
    current = sys.modules.get("numpy")
    if current is None or not hasattr(current, "maximum"):
        sys.modules.pop("numpy", None)
        real_np = importlib.import_module("numpy")
        monkeypatch.setitem(sys.modules, "numpy", real_np)
    else:
        real_np = current
    try:
        import backtest.results as _br

        monkeypatch.setattr(_br, "np", real_np, raising=False)
    except Exception:
        pass

    # ── loguru ─────────────────────────────────────────────────────────────
    # A prior test may have replaced ``loguru.logger`` with a MagicMock, or even
    # swapped the whole ``loguru`` module for a MagicMock (no teardown). Either
    # way the structured-logging file sinks become no-ops. Force-import a real
    # loguru, point this module + the ops logging module at its logger, and
    # restore the original (mock) module afterwards so downstream mock-asserting
    # tests still see what they expect.
    import types as _types

    saved_loguru = sys.modules.get("loguru")
    logger_obj = getattr(saved_loguru, "logger", None)
    polluted = (
        not isinstance(saved_loguru, _types.ModuleType)
        or type(logger_obj).__module__.startswith("unittest")
    )
    if polluted:
        saved_submods = {
            k: sys.modules[k]
            for k in list(sys.modules)
            if k == "loguru" or k.startswith("loguru.")
        }
        for k in saved_submods:
            sys.modules.pop(k, None)
        real_loguru = importlib.import_module("loguru")
        real_logger = real_loguru.logger

        import ops.logging_config as _lc

        monkeypatch.setattr(_lc, "logger", real_logger, raising=False)
        monkeypatch.setattr(sys.modules[__name__], "logger", real_logger, raising=False)

        def _restore_loguru():
            for k in [k for k in list(sys.modules) if k == "loguru" or k.startswith("loguru.")]:
                sys.modules.pop(k, None)
            sys.modules.update(saved_submods)

        request.addfinalizer(_restore_loguru)
    yield




# ── helpers ──────────────────────────────────────────────────────────────────


def _run_backtest(candles_by_pair, *, base_tf="M5", balance=10_000.0):
    """Run the real L7+L8 stack over the given data and return (runner, results)."""
    runner = BacktestRunner(
        data=candles_by_pair,
        base_timeframe=base_tf,
        progress_interval=0,
        starting_balance=balance,
        slippage_pips=0.0,
    )
    results = runner.run()
    return runner, results


def _ops_cfg(tmp_path: Path, **overrides) -> OpsConfig:
    kw = dict(
        crash_marker_path=str(tmp_path / ".crash_marker"),
        heartbeat_file=str(tmp_path / ".heartbeat"),
        main_log=str(tmp_path / "apex.log"),
        trade_audit_log=str(tmp_path / "trade_audit.log"),
        risk_audit_log=str(tmp_path / "risk_audit.log"),
    )
    kw.update(overrides)
    return OpsConfig(**kw)


class _FakeBroker:
    any_connected = True
    _mt5_connected_flags = (True,)
    deriv = None


def _fake_loop(**attrs):
    """A minimal duck-typed stand-in for TradingLoop the ops layer can read."""
    base = dict(
        running=True,
        platforms=_FakeBroker(),
        managed_positions={},
        watchdog=None,
        drawdown=None,
        config=SimpleNamespace(signal_ledger=None, counterfactual=None),
        _signal_ledger=None,
        _post_close_tracker=None,
        _counterfactual=None,
        _interaction_analyzer=None,
        _capital_allocator=None,
        _execution_profiles=None,
        _regime_detector=None,
        _risk_manager=None,
        _behavior_discovery=None,
        _param_evolver=None,
        _signal_discovery=None,
        _module_governor=None,
        _virtual_registry=None,
        _tuner_agent=None,
    )
    base.update(attrs)
    return SimpleNamespace(**base)


# ── Test 1 — full trending day ───────────────────────────────────────────────


def test_full_trading_day_trending():
    runner, res = _run_backtest({"EURUSD": {"M5": sd.trending(candles=800, seed=11)}})
    try:
        m = res.metrics()
        # Valid, finite metrics — never NaN.
        assert isinstance(m["num_trades"], int)
        assert m["num_trades"] >= 0
        assert m["win_rate"] == m["win_rate"]  # not NaN
        assert res.equity_curve, "equity curve must be populated"
        # The real L7 detector classified the pair (any committed regime is fine).
        regime_state = runner.regime_detector.get_state()
        assert regime_state["pair_count"] >= 1
        # The real L8 risk manager tracked the equity curve.
        rm_state = runner.risk_manager.get_state()
        assert "rolling_drawdown_pct" in rm_state
    finally:
        runner.close()


# ── Test 2 — full ranging day ────────────────────────────────────────────────


def test_full_trading_day_ranging():
    runner, res = _run_backtest({"EURUSD": {"M5": sd.ranging(candles=800, seed=13)}})
    try:
        m = res.metrics()
        assert isinstance(m["num_trades"], int)
        assert res.equity_curve
        assert runner.regime_detector.get_state()["pair_count"] >= 1
        # Per-regime breakdown is computable without error.
        assert isinstance(res.per_regime(), dict)
    finally:
        runner.close()


# ── Test 3 — risk gate blocks during drawdown (and logs, never silent) ───────


def test_risk_gate_blocks_during_drawdown(tmp_path):
    from adaptive.risk_manager import RiskManager

    rm = RiskManager(db_path=tmp_path / "risk.db", enabled=True,
                     daily_drawdown_limit_pct=3.0)
    try:
        # Establish the day-start / peak anchor, then breach the daily limit.
        rm.on_trade_closed(0.0, 10_000.0)
        before = rm.can_open_position("EURUSD", "LONG", open_positions=[],
                                      account_balance=10_000.0)
        assert before.allowed, "should allow before any drawdown"

        rm.on_trade_closed(-400.0, 9_600.0)  # 4% daily drawdown > 3% limit
        after = rm.can_open_position("EURUSD", "LONG", open_positions=[],
                                     account_balance=9_600.0)
        assert not after.allowed, "must block once daily drawdown limit breached"
        assert after.rule, "block must carry a structured rule (never silent)"

        state = rm.get_state()
        assert state["daily_drawdown_pct"] >= 3.0
        # The denial is recorded as a risk event — visible, not swallowed.
        assert state["risk_events"], "risk denials must be persisted/visible"
    finally:
        rm.close()


# ── Test 4 — graceful shutdown flushes stores + clears crash marker ──────────


def test_graceful_shutdown(tmp_path):
    class _Store:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    store_a, store_b = _Store(), _Store()
    loop = _fake_loop(_signal_ledger=store_a, _regime_detector=store_b)
    cfg = _ops_cfg(tmp_path)

    # Arm a crash marker as if the run had started.
    StartupRecovery(cfg).write_crash_marker()
    assert Path(cfg.crash_marker_path).exists()

    sm = ShutdownManager(loop, cfg)
    sm.request_shutdown(reason="SIGTERM")
    assert sm.shutting_down
    assert loop.running is False  # new entries suspended

    summary = sm.shutdown(reason="SIGTERM")
    assert store_a.closed and store_b.closed, "every adaptive store must be flushed"
    assert "signal_ledger" in summary["stores_flushed"]
    assert summary["stores_failed"] == []
    assert not Path(cfg.crash_marker_path).exists(), "clean exit clears the marker"

    # Idempotent — a second shutdown does nothing harmful.
    assert sm.shutdown()["already_completed"] is True


# ── Test 5 — startup recovery detects an unclean previous exit ───────────────


def test_startup_recovery(tmp_path):
    cfg = _ops_cfg(tmp_path)
    # Simulate a crash: a marker left behind by a previous run.
    Path(cfg.crash_marker_path).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.crash_marker_path).write_text("pid=999 started=earlier")

    sr = StartupRecovery(cfg)
    summary = sr.run()
    assert summary["unclean_previous_exit"] is True
    assert summary.get("previous_marker")
    # A fresh marker is armed for THIS run.
    assert Path(cfg.crash_marker_path).exists()

    # A clean second boot (after clearing) reports no crash.
    sr.clear_crash_marker()
    summary2 = sr.run()
    assert summary2["unclean_previous_exit"] is False


# ── Test 6 — health check reports every component, incl. drawdown ────────────


def test_health_check(tmp_path):
    from adaptive.regime_detector import RegimeDetector
    from adaptive.risk_manager import RiskManager

    rd = RegimeDetector(db_path=tmp_path / "regime.db", enabled=True)
    rm = RiskManager(db_path=tmp_path / "risk.db", enabled=True)
    rm.on_trade_closed(0.0, 10_000.0)
    loop = _fake_loop(_regime_detector=rd, _risk_manager=rm)
    cfg = _ops_cfg(tmp_path)
    try:
        health = HealthCheck(loop, cfg).get_health()
        assert health["status"] in ("ok", "degraded")
        assert health["status"] != "error"
        # Every adaptive layer reports active/dormant (never missing).
        layers = health["adaptive_layers"]
        assert layers["regime_detector"] == "active"
        assert layers["risk_manager"] == "active"
        assert layers["counterfactual"] == "dormant"
        # Drawdown state is included and sourced from the risk manager.
        assert health["drawdown"]["source"] == "risk_manager"
        assert "rolling_drawdown_pct" in health["drawdown"]
        assert health["broker"]["any_connected"] is True
    finally:
        rd.close()
        rm.close()


# ── Test 7 — all driven layers receive trade data ────────────────────────────


def test_all_layers_receive_trade_data(tmp_path):
    # 7a) The runner-driven L7/L8 layers see real outcomes.
    runner, res = _run_backtest({"EURUSD": {"M5": sd.trend_reversal(candles=500, seed=17)}})
    try:
        assert runner.regime_detector.get_state()["pair_count"] >= 1
        assert isinstance(runner.risk_manager.get_state()["equity_curve"], list)
        # Results captured the trades + equity curve end-to-end.
        assert res.equity_curve
    finally:
        runner.close()

    # 7b) A layer the runner does not wire (behaviour discovery) still records
    #     when fed real trade features directly — proving the recording path.
    from adaptive.behavior_discovery import BehaviorDiscoveryEngine

    bd = BehaviorDiscoveryEngine(enabled=True, db_path=str(tmp_path / "bd.db"),
                                 min_trades_to_cluster=5)
    try:
        for i in range(6):
            bd.record_trade(
                features={
                    "horizon": "SWING",
                    "regime": "TRENDING_UP",
                    "consensus_strength": 0.7,
                    "conviction": 0.6,
                    "score": 70.0,
                    "sl_atr": 2.0,
                    "tp_r": 2.0,
                    "hour": 10.0,
                    "volatility": 0.5,
                    "direction": 1.0,
                },
                r_multiple=0.5 if i % 2 == 0 else -0.3,
            )
        assert bd.total_trades() >= 6
    finally:
        bd.close()


# ── Test 8 — cold start: every layer dormant / no-op, no exceptions ──────────


def test_no_data_all_layers_dormant(tmp_path):
    from adaptive.behavior_discovery import BehaviorDiscoveryEngine
    from adaptive.regime_detector import RegimeDetector
    from adaptive.risk_manager import RiskManager

    rd = RegimeDetector(db_path=tmp_path / "r.db", enabled=True)
    rm = RiskManager(db_path=tmp_path / "rm.db", enabled=True)
    bd = BehaviorDiscoveryEngine(enabled=True, db_path=str(tmp_path / "bd.db"),
                                 min_trades_to_cluster=100)
    try:
        # Cold regime → UNKNOWN at zero confidence, no exception.
        st = rd.update("EURUSD", [])
        assert st.regime == "UNKNOWN"
        # Cold risk gate → allows everything (true no-op).
        dec = rm.can_open_position("EURUSD", "LONG", open_positions=[],
                                   account_balance=10_000.0)
        assert dec.allowed
        # Behaviour discovery below cluster threshold → no behaviours, no error.
        assert bd.total_trades() == 0
        # Health shows everything dormant/active but never 'error'.
        loop = _fake_loop(_regime_detector=rd, _risk_manager=rm, _behavior_discovery=bd)
        health = HealthCheck(loop, _ops_cfg(tmp_path)).get_health()
        assert health["status"] != "error"
    finally:
        rd.close()
        rm.close()
        bd.close()


# ── Test 9 — regime transitions detected with hysteresis ─────────────────────


def test_regime_transitions(tmp_path):
    from adaptive.regime_detector import RegimeDetector

    rd = RegimeDetector(db_path=tmp_path / "r.db", enabled=True)
    try:
        candles = sd.regime_transitions(candles=1200, seed=3)
        seen = set()
        committed = []
        history = []
        for c in candles:
            history.append(c.close)
            st = rd.update("EURUSD", history)
            seen.add(st.regime)
            if not committed or committed[-1] != st.regime:
                committed.append(st.regime)
        # The transition scenario must surface at least two distinct regimes.
        distinct = {r for r in seen if r != "UNKNOWN"}
        assert len(distinct) >= 2, f"expected >= 2 regimes, saw {sorted(seen)}"
        # Hysteresis: far fewer committed switches than bars (no per-bar flip).
        assert len(committed) < len(candles) / 5
    finally:
        rd.close()


# ── Test 10 — multi-pair isolation ───────────────────────────────────────────


def test_multi_pair_isolation(tmp_path):
    data = {
        "EURUSD": {"M5": sd.trending(candles=500, pair="EURUSD", seed=21)},
        "GBPUSD": {"M5": sd.ranging(candles=500, pair="GBPUSD", seed=22)},
    }
    runner, res = _run_backtest(data)
    try:
        state = runner.regime_detector.get_state()
        pairs = {p["pair"] for p in state["pairs"]}
        # Each pair has its own independent regime state.
        assert {"EURUSD", "GBPUSD"}.issubset(pairs)
        # Risk manager tracked a single shared book across both pairs.
        assert isinstance(runner.risk_manager.get_state()["equity_curve"], list)
        # Per-pair breakdown is independent (no cross-contamination of keys).
        per_pair = res.per_pair()
        assert set(per_pair).issubset({"EURUSD", "GBPUSD"})
    finally:
        runner.close()


# ── Test 11 — structured logging output is valid JSON + audit streams ────────


def test_structured_logging_output(tmp_path):
    # Isolate loguru from the rest of the (handler-accumulating) suite: snapshot
    # nothing, clear ALL handlers for a clean core, then restore a basic stderr
    # sink afterwards. This makes the file-sink behaviour deterministic
    # regardless of how many leftover sinks earlier test modules added.
    import sys as _sys

    logger.remove()
    cfg = _ops_cfg(tmp_path, log_format="json")
    summary = configure_structured_logging(cfg)
    sink_ids = summary.get("sink_ids", [])
    try:
        logger.bind(component="integration_test", event="probe").info("structured probe line")
        audit_trade("opened EURUSD", pair="EURUSD", direction="LONG", lots=0.01)
        audit_risk("blocked GBPUSD", pair="GBPUSD", reason="daily_drawdown_exceeded")
    finally:
        # Flush, then remove our sinks and restore a default console sink so the
        # remaining suite still has somewhere to log.
        try:
            logger.complete()
        except Exception:
            pass
        for sid in sink_ids:
            try:
                logger.remove(sid)
            except Exception:
                pass
        logger.add(_sys.stderr, level="INFO")

    assert Path(cfg.main_log).exists(), (
        f"structured main log file must be created — summary={summary}"
    )
    main_lines = [
        ln for ln in Path(cfg.main_log).read_text().splitlines() if ln.strip()
    ]
    assert main_lines, "main log must contain at least one line"
    # Other components also write to the main sink, so find OUR probe line
    # rather than assuming it is the last one written.
    parsed_lines = []
    for ln in main_lines:
        try:
            parsed_lines.append(json.loads(ln))
        except (ValueError, TypeError):
            continue
    probe = next(
        (p for p in parsed_lines
         if p.get("component") == "integration_test" and p.get("event") == "probe"),
        None,
    )
    assert probe is not None, "structured probe line must be present and valid JSON"
    assert "ts" in probe and "level" in probe

    trade_lines = [ln for ln in Path(cfg.trade_audit_log).read_text().splitlines() if ln.strip()]
    risk_lines = [ln for ln in Path(cfg.risk_audit_log).read_text().splitlines() if ln.strip()]
    assert any(json.loads(ln).get("pair") == "EURUSD" for ln in trade_lines)
    assert any(json.loads(ln).get("reason") == "daily_drawdown_exceeded" for ln in risk_lines)


# ── Test 12 — SQLite stores persist across reopen ────────────────────────────


def test_sqlite_stores_persist(tmp_path):
    from adaptive.risk_manager import RiskManager

    db = tmp_path / "risk_persist.db"
    rm = RiskManager(db_path=db, enabled=True)
    rm.on_trade_closed(0.0, 10_000.0)
    rm.on_trade_closed(-150.0, 9_850.0)
    peak_before = rm.get_state()["peak_equity"]
    rm.close()

    # The DB file exists on disk and is non-trivial.
    assert db.exists() and db.stat().st_size > 0

    # Reopening restores the persisted equity / peak state.
    rm2 = RiskManager(db_path=db, enabled=True)
    try:
        state = rm2.get_state()
        assert abs(state["peak_equity"] - peak_before) <= max(1e-6, abs(peak_before) * 1e-6)
        assert isinstance(state["equity_curve"], list)
        assert len(state["equity_curve"]) >= 2, "equity rows must survive a reopen"
    finally:
        rm2.close()
