"""
APEX TRADER — SymbolMapper Tests
Validates broker-agnostic symbol translation for Deriv and IC Markets.
"""

import pytest

from brain.symbol_mapper import SymbolMapper


class TestSymbolMapperDeriv:

    @pytest.fixture
    def mapper(self):
        return SymbolMapper("deriv")

    def test_forex_rule_eurusd(self, mapper):
        assert mapper.to_broker("EURUSD") == "frxEURUSD"

    def test_forex_rule_gbpusd(self, mapper):
        assert mapper.to_broker("GBPUSD") == "frxGBPUSD"

    def test_forex_rule_usdjpy(self, mapper):
        assert mapper.to_broker("USDJPY") == "frxUSDJPY"

    def test_forex_rule_lowercase_input(self, mapper):
        assert mapper.to_broker("eurusd") == "frxEURUSD"

    def test_override_v75(self, mapper):
        assert mapper.to_broker("V75_1S") == "1HZ75V"

    def test_override_v10(self, mapper):
        assert mapper.to_broker("V10_1S") == "1HZ10V"

    def test_override_v100(self, mapper):
        assert mapper.to_broker("V100_1S") == "1HZ100V"

    def test_override_boom1000(self, mapper):
        assert mapper.to_broker("BOOM1000") == "BOOM1000"

    def test_override_crash500(self, mapper):
        assert mapper.to_broker("CRASH500") == "CRASH500"

    def test_override_stpidx(self, mapper):
        assert mapper.to_broker("STPIDX") == "stpRNG"

    def test_override_rngbull(self, mapper):
        assert mapper.to_broker("RNGBULL") == "RDBULL"

    def test_override_rngbear(self, mapper):
        assert mapper.to_broker("RNGBEAR") == "RDBEAR"

    def test_override_xauusd(self, mapper):
        assert mapper.to_broker("XAUUSD") == "frxXAUUSD"

    def test_override_xagusd(self, mapper):
        assert mapper.to_broker("XAGUSD") == "frxXAGUSD"

    def test_unknown_symbol_passthrough(self, mapper):
        assert mapper.to_broker("UNKNOWN123") == "UNKNOWN123"

    def test_override_priority_over_rule(self, mapper):
        result = mapper.to_broker("V75_1S")
        assert result == "1HZ75V"
        assert result != "frxV75_1S"

    def test_reverse_forex(self, mapper):
        mapper.to_broker("EURUSD")
        assert mapper.to_canonical("frxEURUSD") == "EURUSD"

    def test_reverse_synthetic_override(self, mapper):
        assert mapper.to_canonical("1HZ75V") == "V75_1S"

    def test_reverse_unknown_passthrough(self, mapper):
        assert mapper.to_canonical("NOTABROKER") == "NOTABROKER"

    def test_broker_property(self, mapper):
        assert mapper.broker == "deriv"

    def test_all_synthetics_map(self, mapper):
        synthetics = [
            "V10_1S", "V25_1S", "V50_1S", "V75_1S", "V100_1S",
            "BOOM500", "BOOM1000",
            "CRASH500", "CRASH1000",
        ]
        for sym in synthetics:
            result = mapper.to_broker(sym)
            assert result is not None, f"{sym} should produce a valid mapping"


class TestSymbolMapperICMarkets:

    @pytest.fixture
    def mapper(self):
        return SymbolMapper("icmarkets")

    def test_forex_passthrough(self, mapper):
        assert mapper.to_broker("EURUSD") == "EURUSD"

    def test_gold_passthrough(self, mapper):
        assert mapper.to_broker("XAUUSD") == "XAUUSD"

    def test_unknown_passthrough(self, mapper):
        assert mapper.to_broker("USDJPY") == "USDJPY"

    def test_broker_property(self, mapper):
        assert mapper.broker == "icmarkets"

    def test_xtiusd_maps_to_usousd(self, mapper):
        assert mapper.to_broker("XTIUSD") == "USOUSD"

    def test_us100_maps_to_nas100(self, mapper):
        assert mapper.to_broker("US100") == "NAS100"

    def test_us30_maps_to_dj30(self, mapper):
        assert mapper.to_broker("US30") == "DJ30"

    def test_xbrusd_maps_to_ukousd(self, mapper):
        assert mapper.to_broker("XBRUSD") == "UKOUSD"

    def test_jp225_maps_to_jpn225(self, mapper):
        assert mapper.to_broker("JP225") == "JPN225"

    def test_us500_maps_to_sp500(self, mapper):
        assert mapper.to_broker("US500") == "SP500"

    def test_reverse_icmarkets_override(self, mapper):
        assert mapper.to_canonical("NAS100") == "US100"
        assert mapper.to_canonical("USOUSD") == "XTIUSD"
        assert mapper.to_canonical("DJ30") == "US30"


class TestSymbolMapperUnknownBroker:

    def test_unknown_broker_uses_passthrough(self):
        mapper = SymbolMapper("nonexistent_broker")
        assert mapper.to_broker("EURUSD") == "EURUSD"
        assert mapper.to_broker("V75_1S") == "V75_1S"

    def test_unknown_broker_reverse_passthrough(self):
        mapper = SymbolMapper("nonexistent_broker")
        assert mapper.to_canonical("frxEURUSD") == "frxEURUSD"
