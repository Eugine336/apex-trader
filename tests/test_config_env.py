"""Tests for the typed operator environment configuration surface."""

from __future__ import annotations

import pytest

from config import AppConfig
from config_env import ENV_CONFIG_FIELDS


def test_operational_environment_overrides_are_typed(monkeypatch):
    monkeypatch.setenv("APEX_PERFORMANCE_SCAN_INTERVAL_ACTIVE", "7.5")
    monkeypatch.setenv("APEX_OPS_TICK_PROFILING_ENABLED", "false")
    monkeypatch.setenv("APEX_DATA_BACKUP_EXCLUDE_PATTERNS", '["*.tmp","cache/*"]')
    monkeypatch.setenv("APEX_BACKTEST_DEFAULT_SPREAD_PIPS", "1.25")

    config = AppConfig()

    assert config.performance.scan_interval_active == 7.5
    assert config.ops.tick_profiling_enabled is False
    assert config.data_backup.exclude_patterns == ["*.tmp", "cache/*"]
    assert config.backtest.default_spread_pips == 1.25


def test_invalid_environment_value_fails_startup(monkeypatch):
    monkeypatch.setenv("APEX_PERFORMANCE_PARALLEL_SCAN_ENABLED", "sometimes")
    with pytest.raises(ValueError, match="must be a boolean"):
        AppConfig()


def test_invalid_operational_range_fails_startup(monkeypatch):
    monkeypatch.setenv("APEX_PERFORMANCE_SCAN_INTERVAL_ACTIVE", "0")
    with pytest.raises(ValueError, match="must be positive"):
        AppConfig()


def test_owner_limits_and_analytical_thresholds_are_not_generic_env_overrides(monkeypatch):
    monkeypatch.setenv("APEX_RISK_RISK_PER_TRADE_PCT", "99")
    monkeypatch.setenv("APEX_SCORING_MIN_ENTRY_SCORE", "0")
    config = AppConfig()

    assert "risk" not in ENV_CONFIG_FIELDS
    assert "scoring" not in ENV_CONFIG_FIELDS
    assert config.owner_limits.risk_per_trade_pct != 99
    assert config.scoring.min_entry_score != 0


def test_all_environment_allowlist_sections_exist_on_app_config():
    config = AppConfig()
    for section, names in ENV_CONFIG_FIELDS.items():
        target = getattr(config, section)
        for name in names:
            assert hasattr(target, name), f"{section}.{name} is not a config field"
