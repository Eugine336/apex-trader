from pathlib import Path

from config import RiskConfig
from execution.position_worker import WorkerConfig


def test_live_risk_config_cannot_enable_legacy_mechanical_exits(monkeypatch):
    monkeypatch.setenv("RISK_LEGACY_MECHANICAL_EXIT_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("RISK_SCAN_DIRECTIONAL_EXITS_ENABLED", "true")

    risk = RiskConfig()
    worker = WorkerConfig()

    assert not hasattr(risk, "legacy_mechanical_exit_fallback_enabled")
    assert not hasattr(risk, "scan_directional_exits_enabled")
    assert not hasattr(worker, "legacy_mechanical_exit_fallback_enabled")
    assert worker.discretionary_exits_enabled is False
    assert worker.scan_directional_exits_enabled is False

    # The production bootstrap owns the hard-off setting; environment values
    # cannot re-enable the test/backtest-only deterministic suite.
    source = (Path(__file__).resolve().parents[1] / "event_driven_bootstrap.py").read_text(
        encoding="utf-8"
    )
    assert "cfg.discretionary_exits_enabled = False" in source
    assert "cfg.scan_directional_exits_enabled = False" in source
    assert 'getattr(risk_cfg, "legacy_mechanical_exit_fallback_enabled"' not in source
