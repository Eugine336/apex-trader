"""Deriv market-context specs expose only broker-discovered constraints."""
import time
from types import SimpleNamespace

from platforms.deriv.deriv_connector import DerivConnector


def _connector(observed_at):
    connector = DerivConnector.__new__(DerivConnector)
    connector._mapper = SimpleNamespace(to_broker=lambda symbol: symbol)
    connector._discovered_multipliers = {"R_75": [100, 200, 300]}
    connector._discovered_multipliers_at = {"R_75": observed_at}
    return connector


def test_fresh_broker_discovered_multipliers_are_explicitly_verified():
    spec = _connector(time.time()).get_symbol_spec("R_75")
    assert spec["available_multipliers"] == [100, 200, 300]
    assert spec["contract_constraints_verified"] is True
    assert spec["source_verified"] is True
    assert spec["constraints_observed_epoch"] > 0
    assert "trade_tick_value" not in spec
    assert "trade_contract_size" not in spec


def test_stale_broker_discovered_multipliers_are_not_currently_verified():
    spec = _connector(time.time() - 901).get_symbol_spec("R_75")
    assert spec["available_multipliers"] == [100, 200, 300]
    assert spec["contract_constraints_verified"] is False
    assert spec["source_verified"] is True


def test_missing_broker_discovery_remains_missing():
    connector = DerivConnector.__new__(DerivConnector)
    connector._mapper = SimpleNamespace(to_broker=lambda symbol: symbol)
    connector._discovered_multipliers = {}
    connector._discovered_multipliers_at = {}
    assert connector.get_symbol_spec("R_75") == {}


def test_future_dated_broker_constraints_are_not_currently_verified():
    spec = _connector(time.time() + 60).get_symbol_spec("R_75")
    assert spec["available_multipliers"] == [100, 200, 300]
    assert spec["contract_constraints_verified"] is False
    assert spec["source_verified"] is True
