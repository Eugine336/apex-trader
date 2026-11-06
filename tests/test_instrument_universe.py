import pytest

from platforms.instrument_universe import UniverseMode, UniverseSelection, resolve_universe


def test_all_mode_uses_broker_discovery_not_static_registry():
    selection = UniverseSelection.build("all")
    result = resolve_universe(selection, ["EURUSD", "XAUUSD", "US100", "BROKER_CUSTOM"])
    assert result.valid
    assert result.selected_symbols == ("EURUSD", "XAUUSD", "US100", "BROKER_CUSTOM")


def test_selected_mode_preserves_broker_spelling_and_owner_scope():
    selection = UniverseSelection.build("selected", ["eurusd", "xauusd"])
    result = resolve_universe(selection, ["EURUSD.a", "EURUSD", "XAUUSD", "GBPUSD"])
    assert result.valid
    assert result.selected_symbols == ("EURUSD", "XAUUSD")


def test_missing_selected_symbol_fails_closed_instead_of_silently_trading_subset():
    selection = UniverseSelection.build("selected", ["EURUSD", "GBPUSD"])
    result = resolve_universe(selection, ["EURUSD"])
    assert not result.valid
    assert not result.trading_enabled
    assert result.selected_symbols == ("EURUSD",)
    assert result.unavailable_symbols == ("GBPUSD",)


def test_single_mode_requires_exactly_one_symbol():
    selection = UniverseSelection.build("single", ["XAUUSD"])
    result = resolve_universe(selection, ["XAUUSD", "EURUSD"])
    assert result.valid
    assert result.selected_symbols == ("XAUUSD",)


@pytest.mark.parametrize(
    ("mode", "symbols"),
    [("all", ["EURUSD"]), ("selected", []), ("single", []), ("single", ["EURUSD", "GBPUSD"])],
)
def test_invalid_selection_is_rejected(mode, symbols):
    with pytest.raises(ValueError):
        UniverseSelection.build(mode, symbols)


def test_duplicate_symbols_are_normalized_and_removed():
    selection = UniverseSelection.build("selected", ["eurusd", "EURUSD", " xauusd "])
    assert selection.symbols == ("EURUSD", "XAUUSD")


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        UniverseSelection.build("gold-only", ["XAUUSD"])


def test_full_is_an_alias_for_all():
    assert UniverseSelection.build("full").mode is UniverseMode.ALL
