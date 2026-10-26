"""The legacy mechanical exit suite must require an explicit opt-in."""

from config import RiskConfig
from execution.position_worker import WorkerConfig


def test_legacy_mechanical_fallback_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("RISK_LEGACY_MECHANICAL_EXIT_FALLBACK_ENABLED", raising=False)
    monkeypatch.delenv("RISK_SCAN_DIRECTIONAL_EXITS_ENABLED", raising=False)

    risk = RiskConfig()
    worker = WorkerConfig()

    assert risk.legacy_mechanical_exit_fallback_enabled is False
    assert risk.scan_directional_exits_enabled is False
    assert worker.discretionary_exits_enabled is False
    assert worker.scan_directional_exits_enabled is False


def test_legacy_directional_subset_cannot_enable_fallback_by_itself(monkeypatch):
    monkeypatch.delenv("RISK_LEGACY_MECHANICAL_EXIT_FALLBACK_ENABLED", raising=False)
    monkeypatch.setenv("RISK_SCAN_DIRECTIONAL_EXITS_ENABLED", "true")

    risk = RiskConfig()

    assert risk.scan_directional_exits_enabled is True
    assert risk.legacy_mechanical_exit_fallback_enabled is False
    # The explicit fallback switch is the controlling permission. The sub-switch
    # alone is not enough to let the worker take a mechanical discretionary exit.
