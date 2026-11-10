import pytest
from pydantic import ValidationError

from api.models import TradingConfig
from config import AppConfig


def test_app_config_all_mode_keeps_universe_broad():
    config = AppConfig(universe_mode="all")
    assert "EURUSD" in config.enabled_pairs
    assert "XAUUSD" in config.enabled_pairs
    assert "synthetic" in config.enabled_categories
    assert config.universe_mode == "all"


def test_app_config_selected_mode_uses_only_owner_symbols():
    config = AppConfig(universe_mode="selected", enabled_symbols_override=["eurusd", "xauusd", "EURUSD"])
    assert config.enabled_pairs == ["EURUSD", "XAUUSD"]


def test_app_config_single_mode_uses_exactly_one_owner_symbol():
    config = AppConfig(universe_mode="single", enabled_symbols_override=["GBPJPY"])
    assert config.enabled_pairs == ["GBPJPY"]


def test_legacy_explicit_symbol_override_migrates_to_selected_mode():
    config = AppConfig(enabled_symbols_override=["XAUUSD"])
    assert config.universe_mode == "selected"
    assert config.enabled_pairs == ["XAUUSD"]


def test_api_rejects_selected_mode_without_symbols():
    with pytest.raises(ValidationError):
        TradingConfig(universe_mode="selected", enabled_symbols_override=[])


def test_api_rejects_multiple_symbols_in_single_mode():
    with pytest.raises(ValidationError):
        TradingConfig(universe_mode="single", enabled_symbols_override=["EURUSD", "GBPUSD"])


def test_api_rejects_symbol_restrictions_in_all_mode():
    with pytest.raises(ValidationError):
        TradingConfig(universe_mode="all", enabled_symbols_override=["EURUSD"])


def test_all_mode_uses_live_broker_supported_universe_after_discovery():
    config = AppConfig(universe_mode="all")
    config.broker_discovery_complete = True
    config.broker_enabled_symbols = ["EURUSD", "XAUUSD"]
    assert config.enabled_pairs == ["EURUSD", "XAUUSD"]


def test_selected_mode_fails_closed_when_any_requested_symbol_is_unavailable():
    config = AppConfig(universe_mode="selected", enabled_symbols_override=["EURUSD", "GBPUSD"])
    config.broker_discovery_complete = True
    config.broker_enabled_symbols = ["EURUSD"]
    assert config.enabled_pairs == []


def test_instance_override_applies_owner_universe_mode_and_normalizes_symbols():
    from api.instance_config import apply_user_overrides

    config = AppConfig()
    apply_user_overrides(config, {
        "universe_mode": "single",
        "enabled_symbols_override": [" xauusd "],
    })
    assert config.universe_mode == "single"
    assert config.enabled_symbols_override == ["XAUUSD"]
    assert config.enabled_pairs == ["XAUUSD"]


def test_legacy_instance_override_migrates_to_selected_universe():
    from api.instance_config import apply_user_overrides

    config = AppConfig()
    apply_user_overrides(config, {"enabled_symbols_override": ["EURUSD"]})
    assert config.universe_mode == "selected"
    assert config.enabled_symbols_override == ["EURUSD"]


def test_trading_mode_env_selects_gold_only(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "gold")
    config = AppConfig()
    assert config.universe_mode == "single"
    assert config.enabled_symbols_override == ["XAUUSD"]
    assert config.enabled_pairs == ["XAUUSD"]
    assert config.trading_mode == "gold"


def test_trading_mode_env_selects_full_universe(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "full")
    monkeypatch.setenv("TRADING_ENABLED_CATEGORIES", "commodity")
    config = AppConfig()
    assert config.universe_mode == "all"
    assert config.enabled_categories == ["commodity"]


def test_trading_mode_env_requires_symbols_for_selected_mode(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "selected")
    monkeypatch.setenv("TRADING_SYMBOLS", "xauusd,eurusd")
    config = AppConfig()
    assert config.universe_mode == "selected"
    assert config.enabled_symbols_override == ["XAUUSD", "EURUSD"]


def test_trading_enabled_false_disables_new_origination_only(monkeypatch):
    monkeypatch.setenv("TRADING_ENABLED", "false")
    monkeypatch.setenv("COGNITION_ORIGINATION_MODE", "live")
    config = AppConfig()
    assert config.cognition.origination_mode == "off"
    assert config.cognition.management_mode == "live"


def test_trading_enabled_true_preserves_configured_origination_mode(monkeypatch):
    from config import CognitionConfig

    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("COGNITION_ORIGINATION_MODE", "live")
    cognition = CognitionConfig()
    assert cognition.origination_mode == "live"


def test_process_trading_mode_lock_blocks_persisted_user_universe_override(monkeypatch):
    from api.instance_config import apply_user_overrides

    monkeypatch.setenv("TRADING_MODE", "gold")
    monkeypatch.setenv("TRADING_MODE_LOCKED", "true")
    config = AppConfig()
    apply_user_overrides(config, {
        "universe_mode": "all",
        "enabled_categories": ["forex", "commodity", "index", "crypto"],
        "enabled_symbols_override": [],
    })
    assert config.universe_mode == "single"
    assert config.enabled_symbols_override == ["XAUUSD"]
    assert config.enabled_pairs == ["XAUUSD"]


def test_unlocked_process_trading_mode_allows_persisted_user_override(monkeypatch):
    from api.instance_config import apply_user_overrides

    monkeypatch.setenv("TRADING_MODE", "gold")
    monkeypatch.setenv("TRADING_MODE_LOCKED", "false")
    config = AppConfig()
    apply_user_overrides(config, {
        "universe_mode": "selected",
        "enabled_symbols_override": ["EURUSD"],
    })
    assert config.universe_mode == "selected"
    assert config.enabled_pairs == ["EURUSD"]


def test_full_trading_mode_rejects_empty_category_universe(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "full")
    monkeypatch.setenv("TRADING_ENABLED_CATEGORIES", "")
    with pytest.raises(ValueError, match="at least one enabled category"):
        AppConfig()


def test_full_trading_mode_rejects_unknown_categories(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "full")
    monkeypatch.setenv("TRADING_ENABLED_CATEGORIES", "forex,unobtainium")
    with pytest.raises(ValueError, match="unsupported trading categories"):
        AppConfig()


def test_example_environment_defaults_new_trading_exposure_to_disabled():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    lines = (root / ".env.example").read_text(encoding="utf-8").splitlines()
    assert "TRADING_ENABLED=false" in lines


def test_process_trading_mode_is_locked_by_default_when_explicitly_set(monkeypatch):
    from api.instance_config import apply_user_overrides

    monkeypatch.setenv("TRADING_MODE", "gold")
    monkeypatch.delenv("TRADING_MODE_LOCKED", raising=False)
    config = AppConfig()
    apply_user_overrides(config, {
        "universe_mode": "all",
        "enabled_categories": ["forex", "commodity", "index", "synthetic"],
        "enabled_symbols_override": [],
    })
    assert config.universe_mode == "single"
    assert config.enabled_symbols_override == ["XAUUSD"]
