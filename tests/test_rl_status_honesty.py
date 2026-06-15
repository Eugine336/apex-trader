"""
P9 — RL status honesty: RLBridge must report an unambiguous status_label so a
dormant RL path is never mistaken for an active one. When inactive, rl_delta
must stay 0.
"""

import numpy as np

from rl.bridge import RLBridge


def test_disabled_reports_inactive_disabled():
    bridge = RLBridge(checkpoint="nonexistent.pt", enabled=False)
    assert bridge.status_label == "INACTIVE_DISABLED"
    assert bridge.enabled is False
    assert bridge.status()["status_label"] == "INACTIVE_DISABLED"


def test_enabled_without_checkpoint_reports_no_checkpoint():
    bridge = RLBridge(checkpoint="definitely_missing_checkpoint.pt", enabled=True)
    assert bridge.checkpoint_exists is False
    assert bridge.enabled is False
    assert bridge.status_label == "INACTIVE_NO_CHECKPOINT"


def test_inactive_bridge_passthrough_zero_delta():
    bridge = RLBridge(checkpoint="missing.pt", enabled=True)
    obs = np.zeros((50, 12), dtype=np.float32)
    result = bridge.augment_score(pair="EURUSD", base_score=76.5, obs=obs)
    assert result.rl_delta == 0.0
    assert result.final_score == 76.5
    assert result.vetoed is False


def test_status_dict_exposes_checkpoint_fields():
    bridge = RLBridge(checkpoint="missing.pt", enabled=False)
    status = bridge.status()
    assert status["checkpoint"] == "missing.pt"
    assert status["checkpoint_exists"] is False
    assert "status_label" in status
