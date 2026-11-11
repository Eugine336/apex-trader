"""The old mechanical worker fallback has no runtime configuration or API."""

from pathlib import Path
from inspect import signature

from execution.position_worker import PositionWorker, WorkerConfig


ROOT = Path(__file__).resolve().parents[1]


def test_worker_configuration_has_only_safety_controls():
    assert not hasattr(WorkerConfig, "discretionary_exits_enabled")
    assert not hasattr(WorkerConfig, "scan_directional_exits_enabled")
    assert not hasattr(WorkerConfig, "legacy_mechanical_exit_fallback_enabled")
    assert "scan" not in signature(PositionWorker.evaluate).parameters


def test_live_bootstrap_does_not_toggle_or_construct_a_legacy_fallback():
    source = (ROOT / "event_driven_bootstrap.py").read_text(encoding="utf-8")
    assert "discretionary_exits_enabled" not in source
    assert "scan_directional_exits_enabled" not in source
    assert "_build_scan_context" not in source
