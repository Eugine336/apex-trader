"""Regression guard: scan direction and score are not worker authorities."""

from pathlib import Path
from inspect import signature

from execution.position_worker import PositionWorker, WorkerConfig


ROOT = Path(__file__).resolve().parents[1]


def test_scan_directional_worker_path_is_physically_absent():
    assert not hasattr(WorkerConfig, "scan_directional_exits_enabled")
    assert not hasattr(WorkerConfig, "discretionary_exits_enabled")
    assert not hasattr(PositionWorker, "_check_invalidation")
    assert not hasattr(PositionWorker, "_check_conviction_collapse")
    assert not hasattr(PositionWorker, "_check_htf_candle_close")
    assert "scan" not in signature(PositionWorker.evaluate).parameters


def test_live_bootstrap_never_derives_scan_direction_for_position_worker():
    source = (ROOT / "event_driven_bootstrap.py").read_text(encoding="utf-8")
    assert "_build_scan_context" not in source
    assert "ScanContext" not in source
    assert "scan_ctx" not in source
    assert "score_history.append(current_score)" not in source
