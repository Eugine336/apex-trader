"""APEX TRADER — PR 8 regression tests.

P1 — Activate the legacy protective checks (C19–C22) alongside the Decision
     Engine instead of only in the DecisionEngine-disabled branch:
  * When the strategic engine merely HELD this cycle, the legacy checks run as
    additive safety nets (invalidation, conviction-collapse, HTF candle-close,
    dynamic SL tighten).
  * They are skipped when the engine actively acted (CLOSE/TIGHTEN/BE) or when
    the cycle degraded (engine returned None — the legacy fallback already ran).
  * Each legacy config flag still gates its check independently.
  * Each check is fail-isolated: an exception in one does not suppress the rest.
  * A check that closes the position short-circuits the remaining checks.
  * `_run_decision_engine` returns the verdict Action on success and None when
    it degrades.

P5 — Persist a shadow contract on planner:WAIT so perpetually-deferred setups
     enter the counterfactual / gate-tuner learning, mirroring planner:SKIP.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
import sys
import tempfile
from pathlib import Path

# ── Stub heavy/optional deps before any app imports (sandbox + CI parity) ──
_STUB_MODULES = [
    "torch", "torch.nn", "torch.nn.functional",
    "torch.optim", "torch.distributions",
]
for _mod in _STUB_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from platforms.main_loop import TradingLoop  # noqa: E402
from decision.actions import Action, ManagementDecision  # noqa: E402
from persistence.shadow_store import ShadowStore  # noqa: E402

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


# ── Helpers ────────────────────────────────────────────────────────────────

def _risk_cfg(**overrides):
    cfg = SimpleNamespace(
        continuous_analysis_enabled=True,
        conviction_monitoring_enabled=True,
        htf_reassessment_enabled=True,
        htf_reassess_on_h1_close=True,
        dynamic_sl_tightening_enabled=True,
        invalidation_min_hold_minutes=5.0,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _loop(close_on=None, raise_on=None, **cfg_over):
    """A MagicMock loop wired so the four legacy checks are observable.

    ``close_on`` — name of the check that removes the position (simulating a
    close). ``raise_on`` — name of the check that raises.
    """
    m = MagicMock()
    m.config.risk = _risk_cfg(**cfg_over)
    m.managed_positions = {"oid-1": SimpleNamespace(symbol="EURUSD")}

    def _make(name):
        def _fn(*a, **kw):
            if raise_on == name:
                raise RuntimeError(f"{name} boom")
            if close_on == name:
                m.managed_positions.pop("oid-1", None)
        return MagicMock(side_effect=_fn)

    m._check_invalidation = _make("invalidation")
    m._check_conviction_collapse = _make("conviction")
    m._check_htf_candle_close = _make("htf")
    m._apply_dynamic_sl_tightening = _make("dynamic")
    return m


def _call(m, hold_minutes=60.0, opposing_boost=0):
    pos = m.managed_positions.get("oid-1") or SimpleNamespace(symbol="EURUSD")
    TradingLoop._run_active_legacy_checks(
        m, "oid-1", pos, SimpleNamespace(score=50, direction="NEUTRAL"),
        SimpleNamespace(), NOW,
        hold_minutes=hold_minutes, opposing_boost=opposing_boost,
    )


# ── P1: _run_active_legacy_checks ────────────────────────────────────────────

class TestRunActiveLegacyChecks:
    def test_all_checks_run_when_held(self):
        m = _loop()
        _call(m)
        m._check_invalidation.assert_called_once()
        m._check_conviction_collapse.assert_called_once()
        m._check_htf_candle_close.assert_called_once()
        m._apply_dynamic_sl_tightening.assert_called_once()

    def test_dynamic_sl_is_gated_by_its_flag(self):
        m = _loop(dynamic_sl_tightening_enabled=False)
        _call(m)
        m._apply_dynamic_sl_tightening.assert_not_called()
        # others unaffected
        m._check_invalidation.assert_called_once()
        m._check_conviction_collapse.assert_called_once()
        m._check_htf_candle_close.assert_called_once()

    def test_continuous_analysis_flag_gates_invalidation(self):
        m = _loop(continuous_analysis_enabled=False)
        _call(m)
        m._check_invalidation.assert_not_called()
        m._check_conviction_collapse.assert_called_once()

    def test_conviction_flag_gates_conviction_check(self):
        m = _loop(conviction_monitoring_enabled=False)
        _call(m)
        m._check_conviction_collapse.assert_not_called()
        m._check_invalidation.assert_called_once()

    def test_htf_flag_gates_htf_check(self):
        m = _loop(htf_reassess_on_h1_close=False)
        _call(m)
        m._check_htf_candle_close.assert_not_called()
        m._apply_dynamic_sl_tightening.assert_called_once()

    def test_min_hold_gates_invalidation_and_conviction_only(self):
        m = _loop()
        _call(m, hold_minutes=1.0)  # below invalidation_min_hold_minutes (5)
        m._check_invalidation.assert_not_called()
        m._check_conviction_collapse.assert_not_called()
        # HTF + dynamic SL are not hold-gated
        m._check_htf_candle_close.assert_called_once()
        m._apply_dynamic_sl_tightening.assert_called_once()

    def test_checks_are_fail_isolated(self):
        # invalidation raises — the rest must still run.
        m = _loop(raise_on="invalidation")
        _call(m)
        m._check_conviction_collapse.assert_called_once()
        m._check_htf_candle_close.assert_called_once()
        m._apply_dynamic_sl_tightening.assert_called_once()

    def test_close_short_circuits_remaining_checks(self):
        # invalidation closes the position — later checks must NOT run.
        m = _loop(close_on="invalidation")
        _call(m)
        m._check_invalidation.assert_called_once()
        m._check_conviction_collapse.assert_not_called()
        m._check_htf_candle_close.assert_not_called()
        m._apply_dynamic_sl_tightening.assert_not_called()


# ── P1: _run_decision_engine return contract ─────────────────────────────────

def _de_loop():
    m = MagicMock()
    m._build_trade_context.return_value = object()
    m._situation_engine.assess_open_trade.return_value = SimpleNamespace(
        structure_integrity=0.8, tf_alignment=0.4,
    )
    m._risk_governor = None
    m._decision_journal = None
    m._last_decision_action = {}
    m._last_decision_action_time = {}
    m._degraded_management = {}
    m._degraded_management_escalate_cycles = 3
    m.trade_manager.get_trade.return_value = None
    m.managed_positions = {"oid-1": SimpleNamespace(symbol="EURUSD")}
    return m


def test_run_decision_engine_returns_action_on_success():
    m = _de_loop()
    # The decision now flows through the orchestrator-aware management seam
    # (_decide_management), which wraps decide_management with the live-management
    # round table + legacy fallback. _run_decision_engine returns its action.
    m._decide_management.return_value = ManagementDecision(
        action=Action.HOLD, reason="held",
    )
    pos = SimpleNamespace(symbol="EURUSD", tm_trade_id="tm-1")
    action = TradingLoop._run_decision_engine(
        m, "oid-1", pos, object(), {}, hold_minutes=10.0,
        pressure=0, opposing_boost=0, pressure_details=[], now=NOW,
    )
    assert action == Action.HOLD
    m._execute_management_decision.assert_called_once()


def test_run_decision_engine_returns_none_on_degrade():
    m = _de_loop()
    m._build_trade_context.side_effect = RuntimeError("boom")
    pos = SimpleNamespace(symbol="EURUSD", tm_trade_id="tm-1")
    action = TradingLoop._run_decision_engine(
        m, "oid-1", pos, object(), {}, hold_minutes=10.0,
        pressure=0, opposing_boost=0, pressure_details=[], now=NOW,
    )
    assert action is None
    m._run_legacy_management_fallback.assert_called_once()


# ── P5: planner:WAIT shadow contract persistence ─────────────────────────────

def test_planner_wait_persists_shadow_contract():
    with tempfile.TemporaryDirectory() as tmp:
        store = ShadowStore(Path(tmp) / "shadow.db")
        loop = TradingLoop.__new__(TradingLoop)
        loop._shadow_store = store
        loop._current_cycle_id = None
        loop._current_setup_id = None
        loop.trade_manager = SimpleNamespace(
            tp3_ladder_enabled=False, tp3_r_multiple=3.0,
        )
        signal = SimpleNamespace(
            pair="EURUSD", direction="LONG",
            entry_price=1.10000, stop_loss=1.09800,
            tp1=1.10200, tp2=1.10400,
            position_size_lots=0.10, score=80, entry_timeframe="M5",
        )
        TradingLoop._persist_shadow_contract(
            loop, signal, rejecting_gate="planner:WAIT",
        )
        rows = store.get_all_contracts(rejecting_gate="planner:WAIT")
        assert len(rows) == 1
        assert rows[0].rejecting_gate == "planner:WAIT"
        assert rows[0].source == "planner"
        assert rows[0].symbol == "EURUSD"
        assert rows[0].direction == "LONG"
