from types import SimpleNamespace

import pytest


def _symbol(name, *, trade_mode=4):
    return SimpleNamespace(
        name=name,
        description=f"{name} test",
        path="Forex\\Majors",
        visible=True,
        select=True,
        trade_mode=trade_mode,
        digits=5,
        point=0.00001,
        trade_tick_size=0.00001,
        trade_tick_value=1.0,
        trade_tick_value_profit=1.0,
        trade_tick_value_loss=1.0,
        trade_contract_size=100000.0,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        trade_stops_level=0,
        trade_freeze_level=0,
        currency_base="EUR",
        currency_profit="USD",
        currency_margin="USD",
        swap_long=-1.0,
        swap_short=0.5,
    )


def test_discovery_marks_supported_instruments_and_rejects_unknown_or_disabled(monkeypatch):
    pytest.importorskip("pandas")
    import platforms.mt5.mt5_connector as module

    class FakeMT5:
        SYMBOL_TRADE_MODE_DISABLED = 0
        SYMBOL_TRADE_MODE_LONGONLY = 1
        SYMBOL_TRADE_MODE_SHORTONLY = 2
        SYMBOL_TRADE_MODE_CLOSEONLY = 3
        SYMBOL_TRADE_MODE_FULL = 4

        @staticmethod
        def symbols_get():
            return [
                _symbol("EURUSD"),
                _symbol("BROKER_CUSTOM"),
                _symbol("XAUUSD", trade_mode=0),
            ]

    monkeypatch.setattr(module, "mt5", FakeMT5())
    connector = module.MT5Connector()
    connector._connected = True

    catalog = connector.discover_symbols()
    by_symbol = {row["symbol"]: row for row in catalog}

    assert by_symbol["EURUSD"]["new_position_eligible"] is True
    assert by_symbol["EURUSD"]["apex_symbol"] == "EURUSD"
    assert by_symbol["BROKER_CUSTOM"]["new_position_eligible"] is False
    assert by_symbol["BROKER_CUSTOM"]["apex_supported"] is False
    assert by_symbol["XAUUSD"]["new_position_eligible"] is False
    assert all(row["source_verified"] for row in catalog)


def test_unique_broker_suffix_maps_to_canonical_apex_symbol(monkeypatch):
    pytest.importorskip("pandas")
    import platforms.mt5.mt5_connector as module

    class FakeMT5:
        SYMBOL_TRADE_MODE_DISABLED = 0
        SYMBOL_TRADE_MODE_LONGONLY = 1
        SYMBOL_TRADE_MODE_SHORTONLY = 2
        SYMBOL_TRADE_MODE_CLOSEONLY = 3
        SYMBOL_TRADE_MODE_FULL = 4

        @staticmethod
        def symbols_get():
            return [_symbol("EURUSD.a")]

    monkeypatch.setattr(module, "mt5", FakeMT5())
    connector = module.MT5Connector()
    connector._connected = True

    catalog = connector.discover_symbols()

    assert catalog[0]["apex_symbol"] == "EURUSD"
    assert catalog[0]["broker_symbol_mapping_unique"] is True
    assert catalog[0]["new_position_eligible"] is True
    assert connector.symbol_map("EURUSD") == "EURUSD.a"


def test_ambiguous_broker_suffixes_do_not_enter_trade_universe(monkeypatch):
    pytest.importorskip("pandas")
    import platforms.mt5.mt5_connector as module

    class FakeMT5:
        SYMBOL_TRADE_MODE_DISABLED = 0
        SYMBOL_TRADE_MODE_LONGONLY = 1
        SYMBOL_TRADE_MODE_SHORTONLY = 2
        SYMBOL_TRADE_MODE_CLOSEONLY = 3
        SYMBOL_TRADE_MODE_FULL = 4

        @staticmethod
        def symbols_get():
            return [_symbol("EURUSD.a"), _symbol("EURUSD.raw")]

    monkeypatch.setattr(module, "mt5", FakeMT5())
    connector = module.MT5Connector()
    connector._connected = True

    catalog = connector.discover_symbols()

    assert all(row["apex_symbol"] == "EURUSD" for row in catalog)
    assert all(row["broker_symbol_mapping_unique"] is False for row in catalog)
    assert all(row["new_position_eligible"] is False for row in catalog)
