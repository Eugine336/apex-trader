"""
Tests for dormant-component wiring + system-wide tuning visibility (Parts 2 & 3).

Verifies that the components the audit flagged as dormant/dead-wired are now
properly reachable through the TunerAgent, and that the agent can report the
whole 19-component system and validate its registry at startup.

Covered:
  * PostCloseTrackerConfig defaults OFF; tracker honours the flag
  * PostCloseTracker.record_close + process_pending_checks round-trip
  * PostCloseTrackerTunable drives the tracker only when there is work
  * SignalLedger.attach_trade_outcome links a closed trade's result to its signals
  * ConsumerTunable reports config and is never auto-tuned
  * get_system_tuning_status reports all 19 expected components + the missing set
  * validate_registry warns about missing components and reports them
  * dormant components are reported with role=dormant
"""

import pytest

from adaptive.tunable import TuneContext, TuneFrequency
from adaptive.tunable_adapters import ConsumerTunable, PostCloseTrackerTunable
from adaptive.tuner_agent import EXPECTED_TUNABLES, TunerAgent


@pytest.fixture
def agent(tmp_path):
    a = TunerAgent(enabled=True, audit_db_path=str(tmp_path / "audit.db"))
    yield a
    a.close()


# ── PostCloseTracker config + instantiation ─────────────────────────────────


def test_post_close_config_defaults_off():
    from config import AppConfig, PostCloseTrackerConfig

    assert PostCloseTrackerConfig().enabled is False
    assert AppConfig().post_close_tracker.enabled is False


def test_post_close_tracker_disabled_is_noop(tmp_path):
    from adaptive.post_close_tracker import PostCloseTracker
    from config import PostCloseTrackerConfig

    cfg = PostCloseTrackerConfig(enabled=False, db_path=str(tmp_path / "j.db"))
    tracker = PostCloseTracker(config=cfg, db_path=cfg.db_path)
    assert tracker.enabled is False
    tracker.record_close(
        trade_id="t1", pair="EURUSD", direction="LONG",
        entry_price=1.10, exit_price=1.11,
    )
    assert tracker.pending_count == 0  # nothing scheduled when disabled


def test_post_close_tracker_records_close_when_enabled(tmp_path):
    from adaptive.post_close_tracker import PostCloseTracker
    from config import PostCloseTrackerConfig

    cfg = PostCloseTrackerConfig(enabled=True, db_path=str(tmp_path / "j2.db"))
    tracker = PostCloseTracker(config=cfg, db_path=cfg.db_path)
    assert tracker.enabled is True
    tracker.record_close(
        trade_id="t1", pair="EURUSD", direction="LONG",
        entry_price=1.10, exit_price=1.11, sl_price=1.09, tp_price=1.13,
    )
    assert tracker.pending_count == 1


# ── PostCloseTrackerTunable adapter ──────────────────────────────────────────


class _FakeTracker:
    def __init__(self, enabled, pending):
        self.enabled = enabled
        self.pending_count = pending
        self.processed_with = None

    def process_pending_checks(self, data_source):
        self.processed_with = data_source
        self.pending_count = 0


def test_post_close_adapter_skips_when_disabled():
    t = PostCloseTrackerTunable(_FakeTracker(False, 5), data_source_provider=lambda: object())
    res = t.tune(TuneContext())
    assert res.skipped and "disabled" in res.reason


def test_post_close_adapter_skips_with_no_pending():
    t = PostCloseTrackerTunable(_FakeTracker(True, 0), data_source_provider=lambda: object())
    res = t.tune(TuneContext())
    assert res.skipped and "pending" in res.reason


def test_post_close_adapter_skips_with_no_data_source():
    trk = _FakeTracker(True, 3)
    t = PostCloseTrackerTunable(trk, data_source_provider=lambda: None)
    res = t.tune(TuneContext())
    assert res.skipped and trk.processed_with is None


def test_post_close_adapter_processes_when_due():
    trk = _FakeTracker(True, 3)
    sentinel = object()
    t = PostCloseTrackerTunable(trk, data_source_provider=lambda: sentinel)
    res = t.tune(TuneContext())
    assert trk.processed_with is sentinel
    assert res.changed and not res.skipped
    assert t.frequency == TuneFrequency.PER_SCAN_CYCLE


# ── SignalLedger.attach_trade_outcome (close-path wiring target) ─────────────


def test_attach_trade_outcome_links_result_to_signals(tmp_path):
    from adaptive.signal_ledger import SignalLedger, SignalRecord

    ledger = SignalLedger(db_path=tmp_path / "sl.db")
    sid = ledger.record_signal(SignalRecord(
        pair="EURUSD", emitter="momentum", direction="LONG",
        strength=0.8, price_at_signal=1.10,
    ))
    assert sid
    ledger.record_trade_opened(sid, "trade-1")
    updated = ledger.attach_trade_outcome("trade-1", {"outcome": "WIN", "pnl_r": 1.5})
    assert updated == 1
    rec = ledger.get_signal(sid)
    assert rec["outcome"]["trade_outcome"]["outcome"] == "WIN"


def test_attach_trade_outcome_no_trade_is_noop(tmp_path):
    from adaptive.signal_ledger import SignalLedger

    ledger = SignalLedger(db_path=tmp_path / "sl2.db")
    assert ledger.attach_trade_outcome("missing", {"outcome": "WIN"}) == 0


# ── ConsumerTunable (components 14-19 visibility) ────────────────────────────


def test_consumer_tunable_never_auto_tunes():
    c = ConsumerTunable("risk_engine", lambda: {"mode": "CAUTION"})
    assert c.frequency == TuneFrequency.ON_DEMAND
    assert c.should_tune(TuneContext(force=True)) is False
    res = c.tune(TuneContext())
    assert res.skipped and res.success


def test_consumer_tunable_reports_params():
    c = ConsumerTunable("risk_engine", lambda: {"mode": "CAUTION", "balance": 100.0})
    params = c.get_current_params()
    assert params["role"] == "consumer"
    assert params["mode"] == "CAUTION"
    assert params["balance"] == 100.0


def test_consumer_tunable_dormant_role():
    c = ConsumerTunable("rl_stack", lambda: {"bridge_present": False}, dormant=True)
    assert c.get_current_params()["role"] == "dormant"


def test_consumer_tunable_faulty_provider_safe():
    def boom():
        raise ValueError("nope")

    c = ConsumerTunable("x", boom)
    # Must not raise — falls back to the base role/note.
    assert c.get_current_params()["role"] == "consumer"


# ── System status + startup validation (Part 3) ─────────────────────────────


def test_expected_tunables_is_nineteen():
    assert len(EXPECTED_TUNABLES) == 19


def test_system_status_reports_missing_components(agent):
    status = agent.get_system_tuning_status()
    assert status["agent_enabled"] is True
    assert status["is_sole_authority"] is True
    assert status["expected_count"] == 19
    # Nothing registered yet -> all 19 expected are missing.
    assert len(status["unregistered_expected"]) == 19


def test_system_status_full_registry_has_no_missing(agent):
    for name in EXPECTED_TUNABLES:
        agent.register(ConsumerTunable(name, lambda: {"ok": True}))
    status = agent.get_system_tuning_status()
    assert status["registered_count"] == 19
    assert status["unregistered_expected"] == []
    # Every expected component shows up with its reported params.
    for name in EXPECTED_TUNABLES:
        assert name in status["registered_tunables"]


def test_validate_registry_reports_missing(agent):
    agent.register(ConsumerTunable("risk_engine", lambda: {}))
    summary = agent.validate_registry()
    assert "risk_engine" in summary["registered"]
    assert len(summary["missing"]) == 18
    assert "score_optimizer" in summary["missing"]


def test_validate_registry_flags_invalid_params(agent):
    class BadParams(ConsumerTunable):
        def validate_params(self, params):
            return False, "always invalid"

    agent.register(BadParams("score_optimizer", lambda: {}))
    summary = agent.validate_registry()
    assert any("score_optimizer" in s for s in summary["invalid"])


def test_system_status_includes_bypass_attempts(agent):
    from adaptive.tunable import TuningGuardMixin

    class C(TuningGuardMixin):
        def go(self):
            return "BLOCKED" if self._tuning_blocked("go") else "RAN"

    c = C()
    c.set_tuner_agent(agent)
    c.go()
    status = agent.get_system_tuning_status()
    assert len(status["bypass_attempts"]) == 1
    assert status["bypass_attempts"][0]["component"] == "C"
