import pytest
from pydantic import ValidationError

from api.models import TradingConfig
from config import AppConfig


def test_app_config_all_mode_keeps_universe_broad():
    config = AppConfig(universe_mode="all")
    assert "EURUSD" in config.enabled_pairs
    assert "XAUUSD" in config.enabled_pairs
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
