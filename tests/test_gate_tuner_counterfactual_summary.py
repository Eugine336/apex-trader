"""
P8 — counterfactual observability: GateTuner.summarize() reports per-gate
"would-have-won" stats for EVERY rejecting-gate family present in the shadow
outcomes, including the high-authority gates that are never auto-tuned. It must
change no thresholds.
"""

import os
import tempfile

from adaptive.gate_tuner import GateTuner


def _gt():
    tmp = tempfile.mkdtemp()
    return GateTuner(os.path.join(tmp, "g.json"))


def test_summary_covers_high_authority_gates():
    gt = _gt()
    outcomes = [
        {"rejecting_gate": "decision_engine:SKIP", "outcome": "WIN", "cnt": 30, "avg_r": 1.0},
        {"rejecting_gate": "decision_engine:SKIP", "outcome": "LOSS", "cnt": 10, "avg_r": -1.0},
        {"rejecting_gate": "risk_engine:dd", "outcome": "LOSS", "cnt": 5, "avg_r": -1.0},
    ]
    summary = gt.summarize(outcomes)

    # High-authority gates are summarised even though they are not tunable.
    assert "decision_engine" in summary
    assert summary["decision_engine"]["auto_tuned"] is False
    assert summary["decision_engine"]["rejected_resolved"] == 40
    assert summary["decision_engine"]["would_have_won"] == 30
    assert summary["decision_engine"]["would_have_lost"] == 10
    assert summary["decision_engine"]["would_have_won_rate"] == 0.75
    assert summary["risk_engine"]["auto_tuned"] is False


def test_summary_marks_tunable_gates():
    gt = _gt()
    summary = gt.summarize(
        [{"rejecting_gate": "entry_engine:score", "outcome": "WIN", "cnt": 12, "avg_r": 1.0}]
    )
    assert summary["entry_engine"]["auto_tuned"] is True


def test_partial_counts_as_win():
    gt = _gt()
    summary = gt.summarize(
        [{"rejecting_gate": "planner:SKIP", "outcome": "PARTIAL", "cnt": 4, "avg_r": 0.5}]
    )
    assert summary["planner"]["would_have_won"] == 4
    assert summary["planner"]["would_have_won_rate"] == 1.0


def test_summary_does_not_mutate_offsets():
    gt = _gt()
    gt.summarize(
        [{"rejecting_gate": "decision_engine:SKIP", "outcome": "WIN", "cnt": 500, "avg_r": 1.0}]
    )
    # Observability is read-only: nothing is tuned, no offsets are created.
    assert gt.offset("decision_engine") == 0.0
    assert gt.all_offsets() == {f: 0.0 for f in GateTuner.TUNABLE}


def test_empty_outcomes():
    assert _gt().summarize([]) == {}
