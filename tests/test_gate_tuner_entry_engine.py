"""
Test for #4 — gate-tuner whitelist expansion: the entry-engine score bar is now
a tunable quality gate (loosen when its rejected setups keep winning, tighten
back, clamp to the envelope).
"""

import os
import tempfile

from adaptive.gate_tuner import GateTuner


def _gt():
    tmp = tempfile.mkdtemp()
    return GateTuner(os.path.join(tmp, "g.json"))


def _rows(outcome: str, cnt: int):
    return [{"rejecting_gate": f"entry_engine:score {cnt} < 65", "outcome": outcome, "cnt": cnt, "avg_r": 1.0}]


def test_entry_engine_is_whitelisted():
    assert "entry_engine" in GateTuner.TUNABLE
    assert "entry_engine" in _gt().all_offsets()


def test_loosen_when_rejected_setups_win():
    gt = _gt()
    # 28 WIN / 12 LOSS = 70% over 40 resolved → loosen one step.
    changed = gt.calibrate(
        _rows("WIN", 28) + [{"rejecting_gate": "entry_engine:x", "outcome": "LOSS", "cnt": 12, "avg_r": -1.0}]
    )
    assert gt.offset("entry_engine") == -1.0
    assert any(c[0] == "entry_engine" for c in changed)


def test_envelope_clamp():
    gt = _gt()
    for _ in range(10):
        gt.calibrate([{"rejecting_gate": "entry_engine:x", "outcome": "WIN", "cnt": 100, "avg_r": 1.0}])
    assert gt.offset("entry_engine") == -3.0  # clamped at envelope floor


def test_tighten_back_toward_neutral():
    gt = _gt()
    gt.calibrate([{"rejecting_gate": "entry_engine:x", "outcome": "WIN", "cnt": 100, "avg_r": 1.0}])
    assert gt.offset("entry_engine") == -1.0
    gt.calibrate([{"rejecting_gate": "entry_engine:x", "outcome": "LOSS", "cnt": 100, "avg_r": -1.0}])
    assert gt.offset("entry_engine") == 0.0


def test_min_samples_gate():
    gt = _gt()
    # Only 20 resolved (< MIN_SAMPLES) → no change.
    gt.calibrate([{"rejecting_gate": "entry_engine:x", "outcome": "WIN", "cnt": 20, "avg_r": 1.0}])
    assert gt.offset("entry_engine") == 0.0
