"""Tests for the typed operator environment configuration surface."""

from __future__ import annotations

import pytest

from config import AppConfig
from config_env import ENV_CONFIG_FIELDS


def test_operational_environment_overrides_are_typed(monkeypatch):
    monkeypatch.setenv("APEX_PERFORMANCE_SCAN_INTERVAL_ACTIVE", "7")
    monkeypatch.setenv("APEX_OPS_TICK_PROFILING_ENABLED", "false")
    monkeypatch.setenv("APEX_DATA_BACKUP_EXCLUDE_PATTERNS", '["*.tmp","cache/*"]')
    monkeypatch.setenv("APEX_BACKTEST_DEFAULT_SPREAD_PIPS", "1.25")

    config = AppConfig()

    assert config.performance.scan_interval_active == 7
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


def test_root_application_log_level_is_environment_configurable(monkeypatch):
    monkeypatch.setenv("APEX_LOG_LEVEL", "debug")
    assert AppConfig().log_level == "DEBUG"


def test_invalid_root_application_log_level_fails_startup(monkeypatch):
    monkeypatch.setenv("APEX_LOG_LEVEL", "VERBOSE")
    with pytest.raises(ValueError, match="APEX_LOG_LEVEL"):
        AppConfig()


def test_owner_limits_bootstrap_uses_canonical_validation(monkeypatch):
    monkeypatch.setenv(
        "APEX_OWNER_LIMITS_JSON",
        '{"risk_per_trade_pct":0.5,"max_open_positions":3,"daily_loss_limit_pct":2.0}',
    )
    limits = AppConfig().owner_limits
    assert limits.risk_per_trade_pct == 0.5
    assert limits.max_open_positions == 3
    assert limits.daily_loss_limit_pct == 2.0


def test_owner_limits_environment_cannot_override_internal_hard_stops(monkeypatch):
    monkeypatch.setenv(
        "APEX_OWNER_LIMITS_JSON",
        '{"hard_stop_drawdown_pct":99.0}',
    )
    with pytest.raises(ValueError, match="internal safety/analysis"):
        AppConfig()


def test_owner_limits_environment_cannot_exceed_immutable_risk_ceiling(monkeypatch):
    monkeypatch.setenv(
        "APEX_OWNER_LIMITS_JSON",
        '{"max_risk_pct_per_trade":9.0}',
    )
    with pytest.raises(ValueError, match="5% hard per-trade ceiling"):
        AppConfig()


def test_timeframe_cache_ttl_map_is_typed_and_validated(monkeypatch):
    monkeypatch.setenv(
        "APEX_PERFORMANCE_CANDLE_CACHE_TTL",
        '{"M1":1.5,"M5":4.5}',
    )
    config = AppConfig()
    assert config.performance.candle_cache_ttl == {"M1": 1.5, "M5": 4.5}


def test_invalid_timeframe_cache_ttl_is_rejected(monkeypatch):
    monkeypatch.setenv(
        "APEX_PERFORMANCE_CANDLE_CACHE_TTL",
        '{"M1":0,"M5":4.5}',
    )
    with pytest.raises(ValueError, match="positive finite TTLs"):
        AppConfig()


def test_default_app_config_remains_constructible_without_environment(monkeypatch):
    monkeypatch.delenv("APEX_PERFORMANCE_CANDLE_CACHE_TTL", raising=False)
    config = AppConfig()
    assert config.performance.candle_cache_ttl["M1"] > 0
