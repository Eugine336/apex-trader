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
    config = AppConfig(
        universe_mode="selected",
        enabled_symbols_override=["eurusd", "xauusd", "EURUSD"],
    )
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
