"""Tests for evidence-based position management (Session 28, Gap 3 part 1).

An open position is defended by nothing but the standing evidence: the
ThesisEngine's competing Long/Short/Flat theses are re-read (with decay
applied) on each management cycle, and when the thesis that justified the
position no longer beats the Flat (do-nothing) baseline — or the opposing
thesis has become dominant — the position is exited on EVIDENCE rather than
ridden to the mechanical R-ladder / stop-loss.

Coverage:
* ``ThesisEngine.evaluate_open_position`` — the pure decision (intact / decayed
  / flipped / untracked / fail-safe / short mirror / detail payload).
* ``ExitCause`` taxonomy for the new evidence-driven causes.
* ``ThesisConfig`` evidence-exit defaults + validation.
* ``DecisionJournal.log_management_event`` — self-contained JSONL record.
* ``PositionEvaluator._check_evidence_exit`` — the management-loop gate
  (disabled switch, min-hold, check-interval throttle, cause mapping, no
  engine, fail-safe fall-through to the R-ladder).
* ``EventDrivenSystem._arm_evidence_recheck`` — the continuous (developing)
  hook that clears the per-ticket throttles so an open position is re-checked
  immediately when the thesis moves between candle closes.
"""

from __future__ import annotations

import types

import pytest

import conftest  # noqa: F401 — installs the MetaTrader5 stub for the bootstrap import

from brain.directional_consensus import Vote
from brain.thesis_engine import FLAT, LONG, SHORT, ThesisEngine
from config import ThesisConfig
from management.exit_cause import ExitCause


# ── Helpers ──────────────────────────────────────────────────────────────

def _vote(module: str, direction: str, confidence: float, weight: float = 1.0) -> Vote:
    return Vote(module=module, direction=direction, confidence=confidence, weight=weight)


def _engine(**kw) -> ThesisEngine:
    params = dict(min_ev_threshold=0.1, decay_enabled=True, decay_half_life=100.0)
    params.update(kw)
    return ThesisEngine(**params)


def _feed(engine: ThesisEngine, symbol: str, *, ev_long: float, ev_short: float,
          long_p: float = 0.6, short_p: float = 0.4, votes=None):
    engine.update(
        symbol=symbol,
        votes=votes if votes is not None else [_vote("structure", LONG, 0.9, 2.0)],
        long_probability=long_p,
        short_probability=short_p,
        entry_ev_long=ev_long,
        entry_ev_short=ev_short,
    )
    return engine.get(symbol).long_thesis.last_refreshed_at


# ── A. ThesisEngine.evaluate_open_position — the pure decision ─────────────

def test_intact_thesis_holds():
    eng = _engine()
    t0 = _feed(eng, "EURUSD", ev_long=0.5, ev_short=-0.3)
    signal, reason, detail = eng.evaluate_open_position("EURUSD", "LONG", now=t0)
    assert signal == "HOLD"
    assert detail["own_ev"] > detail["flat_ev"]


def test_decayed_supporting_thesis_triggers_evidence_exit():
    eng = _engine(decay_half_life=60.0)
    t0 = _feed(eng, "EURUSD", ev_long=0.5, ev_short=-0.3)
    # Many half-lives later the LONG effective EV has decayed toward 0, so it no
    # longer beats Flat (0) by the 0.1R margin → the reason to hold is gone.
    signal, reason, detail = eng.evaluate_open_position(
        "EURUSD", "LONG", now=t0 + 1000.0,
    )
    assert signal == "EVIDENCE_EXIT"
    assert detail["own_over_flat"] < detail["threshold"]


def test_competing_thesis_dominance_triggers_flip():
    eng = _engine()
    # SHORT is the dominant, actionable read while we hold LONG.
    t0 = _feed(
        eng, "EURUSD",
        ev_long=-0.2, ev_short=0.5,
        long_p=0.3, short_p=0.7,
        votes=[_vote("structure", SHORT, 0.9, 2.0)],
    )
    signal, reason, detail = eng.evaluate_open_position("EURUSD", "LONG", now=t0)
    assert signal == "THESIS_FLIP"
    assert detail["opp_ev"] > detail["own_ev"]
    assert "competing" in reason.lower()


def test_flip_disabled_does_not_flip():
    eng = _engine()
    t0 = _feed(
        eng, "EURUSD",
        ev_long=-0.2, ev_short=0.5,
        long_p=0.3, short_p=0.7,
        votes=[_vote("structure", SHORT, 0.9, 2.0)],
    )
    # With flip disabled, the dominant opposing SHORT can't raise THESIS_FLIP.
    # The held LONG's own EV (-0.2) is below flat+threshold → EVIDENCE_EXIT.
    signal, _, _ = eng.evaluate_open_position(
        "EURUSD", "LONG", now=t0, flip_exit_enabled=False,
    )
    assert signal == "EVIDENCE_EXIT"


def test_no_thesis_returns_hold():
    eng = _engine()
    # Never exit a position on absent evidence.
    signal, reason, detail = eng.evaluate_open_position("GBPUSD", "LONG")
    assert signal == "HOLD"
    assert reason == "no_thesis"
    assert detail == {}


def test_short_position_mirror():
    eng = _engine(decay_half_life=60.0)
    t0 = _feed(
        eng, "USDJPY",
        ev_long=-0.3, ev_short=0.5,
        long_p=0.3, short_p=0.7,
        votes=[_vote("structure", SHORT, 0.9, 2.0)],
    )
    # Fresh SHORT thesis supports a SHORT position → HOLD.
    assert eng.evaluate_open_position("USDJPY", "SELL", now=t0)[0] == "HOLD"
    # Decayed → EVIDENCE_EXIT.
    assert eng.evaluate_open_position("USDJPY", "SELL", now=t0 + 1000.0)[0] == "EVIDENCE_EXIT"


def test_detail_payload_has_numbers():
    eng = _engine()
    t0 = _feed(eng, "EURUSD", ev_long=0.5, ev_short=-0.3)
    _, _, detail = eng.evaluate_open_position("EURUSD", "LONG", now=t0)
    for key in ("own_ev", "opp_ev", "flat_ev", "own_over_flat", "threshold",
                "dominant", "position_direction"):
        assert key in detail


def test_evaluate_open_position_is_fail_safe():
    eng = _engine()
    _feed(eng, "EURUSD", ev_long=0.5, ev_short=-0.3)
    # Garbage direction must not raise — returns a valid tuple.
    signal, reason, detail = eng.evaluate_open_position("EURUSD", None)
    assert signal in ("HOLD", "EVIDENCE_EXIT", "THESIS_FLIP")


def test_threshold_override_respected():
    eng = _engine(min_ev_threshold=0.1)
    t0 = _feed(eng, "EURUSD", ev_long=0.2, ev_short=-0.3)
    # Own EV over flat = 0.2. With a 0.1 threshold → HOLD.
    assert eng.evaluate_open_position("EURUSD", "LONG", now=t0)[0] == "HOLD"
    # Raise the opportunity-cost bar to 0.3 → 0.2 no longer beats doing nothing.
    assert eng.evaluate_open_position(
        "EURUSD", "LONG", now=t0, opportunity_cost_threshold=0.3,
    )[0] == "EVIDENCE_EXIT"


# ── B. ExitCause taxonomy ──────────────────────────────────────────────────

def test_new_exit_causes_exist():
    assert ExitCause.EVIDENCE_EXIT.value == "evidence_exit"
    assert ExitCause.THESIS_FLIP.value == "thesis_flip"


def test_from_reason_maps_new_causes():
    assert ExitCause.from_reason("evidence_exit") is ExitCause.EVIDENCE_EXIT
    assert ExitCause.from_reason("evidence: LONG thesis no longer supports") is ExitCause.EVIDENCE_EXIT
    assert ExitCause.from_reason("competing SHORT thesis dominant") is ExitCause.THESIS_FLIP
    assert ExitCause.from_reason("thesis_flip") is ExitCause.THESIS_FLIP


# ── C. ThesisConfig evidence-exit defaults + validation ────────────────────

def test_config_defaults():
    c = ThesisConfig()
    assert c.evidence_exit_enabled is True
    assert c.evidence_exit_check_interval == 30.0
    assert c.evidence_exit_min_hold_time == 60.0
    assert c.thesis_flip_exit_enabled is True


@pytest.mark.parametrize("kw", [
    {"evidence_exit_check_interval": -1.0},
    {"evidence_exit_min_hold_time": -5.0},
])
def test_config_validation_rejects_negative(kw):
    with pytest.raises(ValueError):
        ThesisConfig(**kw)


# ── D. DecisionJournal.log_management_event ────────────────────────────────

def test_log_management_event_writes_jsonl(tmp_path):
    import json
    from decision.journal import DecisionJournal

    dj = DecisionJournal(base_dir=str(tmp_path))
    dj.log_management_event(
        symbol="EURUSD", order_id="123", direction="LONG",
        event="evidence_exit", reason="LONG thesis no longer supports",
        profit_r=0.3, pnl_pips=4.2, pnl_dollars=1.1, hold_minutes=7.5,
        detail={"own_ev": 0.02, "flat_ev": 0.0, "threshold": 0.1},
    )
    dj.close()

    files = list(tmp_path.glob("decisions_*.jsonl"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text().strip())
    assert rec["decision_type"] == "MANAGEMENT_EVENT"
    assert rec["event"] == "evidence_exit"
    assert rec["symbol"] == "EURUSD"
    assert rec["order_id"] == "123"
    assert rec["profit_r"] == 0.3
    assert rec["detail"]["threshold"] == 0.1


def test_log_management_event_is_fail_safe():
    # A journal whose file handle is broken must not raise into the caller.
    from decision.journal import DecisionJournal

    dj = DecisionJournal.__new__(DecisionJournal)
    import threading
    dj._base_dir = None            # type: ignore[attr-defined]
    dj._current_date = "1970-01-01"  # avoid rotation attempt
    dj._file = None
    dj._lock = threading.RLock()
    # Should log the summary line and swallow the write no-op without raising.
    dj.log_management_event(
        symbol="X", order_id="1", direction="LONG",
        event="evidence_exit", reason="r",
    )


# ── E. PositionEvaluator._check_evidence_exit (management-loop gate) ────────

def _evaluator_stub(engine, cfg):
    """A minimal stand-in exposing only what ``_check_evidence_exit`` reads."""
    import event_driven_bootstrap as edb

    stub = types.SimpleNamespace(
        _ctx=types.SimpleNamespace(thesis_engine=engine),
        _config=types.SimpleNamespace(thesis=cfg),
        _last_evidence_exit_check={},
    )
    # Bind the real unbound method to the stub.
    stub._check_evidence_exit = types.MethodType(
        edb.PositionEvaluator._check_evidence_exit, stub,
    )
    return stub


def test_check_evidence_exit_disabled_returns_none():
    eng = _engine(decay_half_life=60.0)
    t0 = _feed(eng, "EURUSD", ev_long=0.5, ev_short=-0.3)
    cfg = ThesisConfig(evidence_exit_enabled=False)
    stub = _evaluator_stub(eng, cfg)
    # Even a fully-decayed thesis yields no exit when the switch is off.
    assert stub._check_evidence_exit("123", "EURUSD", "LONG", 10_000.0, t0 + 5000.0) is None


def test_check_evidence_exit_respects_min_hold():
    eng = _engine(decay_half_life=60.0)
    t0 = _feed(eng, "EURUSD", ev_long=0.5, ev_short=-0.3)
    cfg = ThesisConfig(evidence_exit_min_hold_time=60.0)
    stub = _evaluator_stub(eng, cfg)
    # Held only 30s (< 60s min hold) → no evidence exit even if decayed.
    assert stub._check_evidence_exit("123", "EURUSD", "LONG", 30.0, t0 + 5000.0) is None


def test_check_evidence_exit_interval_throttle():
    eng = _engine()
    # Base EV already below flat+threshold (0.05 < 0.1) → an immediate evidence
    # exit that does not depend on wall-clock decay (the helper reads the engine
    # at real time, so we make the verdict time-independent for a deterministic
    # throttle test).
    _feed(eng, "EURUSD", ev_long=0.05, ev_short=-0.3)
    cfg = ThesisConfig(evidence_exit_check_interval=30.0, evidence_exit_min_hold_time=0.0)
    stub = _evaluator_stub(eng, cfg)
    # First call at now_mono=1000 runs the check.
    first = stub._check_evidence_exit("123", "EURUSD", "LONG", 5000.0, 1000.0)
    assert first is not None and first[0] == "evidence_exit"
    # Second call 5s later is throttled (< 30s interval) → None.
    assert stub._check_evidence_exit("123", "EURUSD", "LONG", 5000.0, 1005.0) is None
    # After the interval elapses it runs again.
    assert stub._check_evidence_exit("123", "EURUSD", "LONG", 5000.0, 1040.0) is not None


def test_check_evidence_exit_returns_evidence_cause():
    eng = _engine()
    # Supporting LONG EV (0.05) below the 0.1 opportunity-cost margin over Flat
    # → EVIDENCE_EXIT immediately (no decay dependency).
    _feed(eng, "EURUSD", ev_long=0.05, ev_short=-0.3)
    cfg = ThesisConfig(evidence_exit_min_hold_time=0.0, evidence_exit_check_interval=0.0)
    stub = _evaluator_stub(eng, cfg)
    res = stub._check_evidence_exit("123", "EURUSD", "LONG", 5000.0, 100.0)
    assert res is not None
    cause, reason, detail = res
    assert cause == "evidence_exit"
    assert isinstance(detail, dict)


def test_check_evidence_exit_returns_flip_cause():
    eng = _engine()
    _feed(
        eng, "EURUSD",
        ev_long=-0.2, ev_short=0.5,
        long_p=0.3, short_p=0.7,
        votes=[_vote("structure", SHORT, 0.9, 2.0)],
    )
    cfg = ThesisConfig(evidence_exit_min_hold_time=0.0, evidence_exit_check_interval=0.0)
    stub = _evaluator_stub(eng, cfg)
    res = stub._check_evidence_exit("123", "EURUSD", "LONG", 5000.0, 100.0)
    assert res is not None
    assert res[0] == "thesis_flip"


def test_check_evidence_exit_holds_intact_thesis():
    eng = _engine()
    _feed(eng, "EURUSD", ev_long=0.5, ev_short=-0.3)
    cfg = ThesisConfig(evidence_exit_min_hold_time=0.0, evidence_exit_check_interval=0.0)
    stub = _evaluator_stub(eng, cfg)
    # Healthy supporting thesis → None → the R-ladder path is left intact.
    assert stub._check_evidence_exit("123", "EURUSD", "LONG", 5000.0, 100.0) is None


def test_check_evidence_exit_no_engine_returns_none():
    stub = types.SimpleNamespace(
        _ctx=types.SimpleNamespace(thesis_engine=None),
        _config=types.SimpleNamespace(thesis=ThesisConfig()),
        _last_evidence_exit_check={},
    )
    import event_driven_bootstrap as edb
    stub._check_evidence_exit = types.MethodType(
        edb.PositionEvaluator._check_evidence_exit, stub,
    )
    assert stub._check_evidence_exit("1", "EURUSD", "LONG", 5000.0, 100.0) is None


def test_check_evidence_exit_fail_safe_falls_through():
    class _Boom:
        def get(self, *_a, **_k):
            return object()  # not None → passes the cold-start guard

        def evaluate_open_position(self, *_a, **_k):
            raise RuntimeError("boom")

    cfg = ThesisConfig(evidence_exit_min_hold_time=0.0, evidence_exit_check_interval=0.0)
    stub = _evaluator_stub(_Boom(), cfg)
    # A fault inside the engine must fall through to the R-ladder (None), never
    # raise and never close a trade.
    assert stub._check_evidence_exit("1", "EURUSD", "LONG", 5000.0, 100.0) is None


# ── F. EventDrivenSystem._arm_evidence_recheck (continuous hook) ───────────

def _system_stub(cfg, candidate_positions):
    import event_driven_bootstrap as edb

    evaluator = types.SimpleNamespace(
        _candidate_positions=candidate_positions,
        _last_de_eval={oid: 999.0 for oid in candidate_positions},
        _last_evidence_exit_check={oid: 999.0 for oid in candidate_positions},
    )
    stub = types.SimpleNamespace(
        _config=types.SimpleNamespace(thesis=cfg),
        _evaluator=evaluator,
    )
    stub._arm_evidence_recheck = types.MethodType(
        edb.EventDrivenSystem._arm_evidence_recheck, stub,
    )
    return stub, evaluator


def test_arm_evidence_recheck_clears_throttles_for_symbol():
    cp = {
        "111": types.SimpleNamespace(symbol="EURUSD", direction="LONG"),
        "222": types.SimpleNamespace(symbol="GBPUSD", direction="SHORT"),
    }
    stub, evaluator = _system_stub(ThesisConfig(), cp)
    stub._arm_evidence_recheck("EURUSD")
    # EURUSD ticket reset to 0 (re-check now); GBPUSD untouched.
    assert evaluator._last_de_eval["111"] == 0.0
    assert evaluator._last_evidence_exit_check["111"] == 0.0
    assert evaluator._last_de_eval["222"] == 999.0
    assert evaluator._last_evidence_exit_check["222"] == 999.0


def test_arm_evidence_recheck_disabled_is_noop():
    cp = {"111": types.SimpleNamespace(symbol="EURUSD", direction="LONG")}
    stub, evaluator = _system_stub(ThesisConfig(evidence_exit_enabled=False), cp)
    stub._arm_evidence_recheck("EURUSD")
    # Switch off → throttles left untouched.
    assert evaluator._last_de_eval["111"] == 999.0
    assert evaluator._last_evidence_exit_check["111"] == 999.0
