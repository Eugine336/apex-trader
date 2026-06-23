"""Tests for per-user AppConfig overrides and environment override loading."""

import json

from api.instance_config import (
    apply_user_overrides,
    build_instance_environment,
    load_user_overrides_from_env,
    write_user_config,
)
from config import AppConfig


def test_apply_user_overrides_mutates_config():
    cfg = AppConfig()
    apply_user_overrides(
        cfg,
        {
            "enabled_symbols_override": ["EURUSD", "GBPUSD"],
            "risk_per_trade_pct": 1.5,
            "max_daily_drawdown_pct": 5.0,
            "max_open_trades": 3,
            "data_path_fixes_enabled": False,
            "log_level": "debug",
            "unknown_key": "ignored",
        },
    )
    assert cfg.enabled_symbols_override == ["EURUSD", "GBPUSD"]
    assert cfg.risk.risk_per_trade_pct == 1.5
    assert cfg.risk.max_daily_drawdown_pct == 5.0
    assert cfg.risk.max_open_trades == 3
    assert cfg.features.data_path_fixes_enabled is False
    assert cfg.log_level == "DEBUG"
    # Per-user instances must not run the git/clean-start machinery.
    assert cfg.data_backup.enabled is False
    assert cfg.data_backup.auto_sync_data_repo is False
    assert cfg.data_backup.clean_start_on_first_boot is False


def test_apply_overrides_keeps_weekly_dd_consistent():
    cfg = AppConfig()
    # Setting daily above the default weekly cap should bump weekly to match.
    apply_user_overrides(cfg, {"max_daily_drawdown_pct": 50.0})
    assert cfg.risk.max_weekly_drawdown_pct >= 50.0


def test_apply_empty_overrides_is_noop():
    cfg = AppConfig()
    risk_before = cfg.risk.risk_per_trade_pct
    apply_user_overrides(cfg, {})
    assert cfg.risk.risk_per_trade_pct == risk_before


def test_write_and_load_user_config(tmp_path, monkeypatch):
    cfg_path = tmp_path / "user_config.json"
    write_user_config(cfg_path, {"risk_per_trade_pct": 2.0, "bogus": 1})
    data = json.loads(cfg_path.read_text())
    assert data == {"risk_per_trade_pct": 2.0}  # bogus filtered out

    monkeypatch.setenv("APEX_USER_CONFIG", str(cfg_path))
    assert load_user_overrides_from_env() == {"risk_per_trade_pct": 2.0}


def test_load_overrides_missing_env_returns_empty(monkeypatch):
    monkeypatch.delenv("APEX_USER_CONFIG", raising=False)
    assert load_user_overrides_from_env() == {}


def test_build_environment_without_deriv(tmp_path):
    env = build_instance_environment(
        user_id=3,
        workdir=tmp_path / "u3",
        config_path=tmp_path / "u3" / "cfg.json",
        api_db_path=tmp_path / "api.db",
        broker_credentials={"mt5": {"login": 5, "password": "p", "server": "s"}},
        base_env={},
    )
    assert "DERIV_ACCESS_TOKEN" not in env
    assert env["MT5_LOGIN"] == "5"


def test_build_environment_strips_whitespace_credentials(tmp_path):
    """Defense-in-depth: padded credentials (e.g. stored pre-fix) are cleaned
    before being injected into the trading subprocess environment."""
    env = build_instance_environment(
        user_id=4,
        workdir=tmp_path / "u4",
        config_path=tmp_path / "u4" / "cfg.json",
        api_db_path=tmp_path / "api.db",
        broker_credentials={
            "mt5": {"login": " 5 ", "password": "  p \n", "server": " Broker-Live "},
            "deriv": {
                "access_token": "  tok\t",
                "app_id": " 1089 ",
                "account_type": " demo ",
                "client_id": "  cid ",
            },
        },
        base_env={},
    )
    assert env["MT5_LOGIN"] == "5"
    assert env["MT5_PASSWORD"] == "p"
    assert env["MT5_SERVER"] == "Broker-Live"
    assert json.loads(env["MT5_BROKERS"]) == [
        {"login": 5, "password": "p", "server": "Broker-Live"}
    ]
    assert env["DERIV_ACCESS_TOKEN"] == "tok"
    assert env["DERIV_APP_ID"] == "1089"
    assert env["DERIV_ACCOUNT_TYPE"] == "demo"
    assert env["DERIV_CLIENT_ID"] == "cid"
