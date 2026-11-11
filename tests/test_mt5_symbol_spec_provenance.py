from __future__ import annotations

from types import SimpleNamespace

import platforms.mt5.mt5_connector as connector_module
from platforms.mt5.clock import MT5ServerClockState
from platforms.mt5.mt5_connector import MT5Connector


def _connector(monkeypatch, *, clock_age_seconds=0.0):
    now = connector_module._time.monotonic()
    state = MT5ServerClockState(
        server="Broker-Demo",
        offset_seconds=7200.0,
        observed_server_epoch=1_800_000_000,
        observed_gmt_epoch=1_799_992_800,
        file_age_seconds=0.0,
        observed_monotonic=now - clock_age_seconds,
        uncertainty_seconds=0.6,
        sources=("ntp:time.cloudflare.com", "https:time.google.com"),
    )
    info = SimpleNamespace(
        swap_long=-2.0,
        swap_short=-1.0,
        swap_mode=0,
        swap_rollover3days=3,
        swap_sunday=0,
        swap_monday=1,
        swap_tuesday=1,
        swap_wednesday=3,
        swap_thursday=1,
        swap_friday=1,
        swap_saturday=0,
        trade_tick_value=1.0,
        trade_tick_size=0.01,
        trade_contract_size=100.0,
        trade_mode=4,
        trade_stops_level=0,
        trade_freeze_level=0,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        digits=2,
        point=0.01,
        currency_base="XAU",
        currency_profit="USD",
        currency_margin="USD",
        description="Gold",
        path="Metals\\Gold",
        trade_calc_mode=0,
        trade_exemode=0,
        filling_mode=1,
        order_mode=127,
        expiration_mode=0,
        start_time=0,
        expiration_time=0,
    )
    fake_mt5 = SimpleNamespace(
        symbol_info=lambda _symbol: info,
        SYMBOL_SWAP_MODE_DISABLED=0,
    )
    monkeypatch.setattr(connector_module, "mt5", fake_mt5)
    connector = MT5Connector.__new__(MT5Connector)
    connector._require_connection = lambda: None
    connector.symbol_map = lambda symbol: symbol
    connector._get_symbol_sessions = lambda _symbol: {}
    connector._server_clock_state_cache = state
    connector._server_clock_state_cache_at = now - clock_age_seconds
    return connector


def test_symbol_spec_timestamp_uses_fresh_synchronized_broker_clock(monkeypatch):
    connector = _connector(monkeypatch)

    spec = connector.get_symbol_spec("XAUUSD")

    assert spec["source_verified"] is True
    assert spec["specification_verified"] is True
    assert spec["source_timestamp_verified"] is True
    assert spec["source_timestamp_epoch"] > 0.0
    assert spec["source_timestamp_semantics"] == "broker_server_observation_time"
    assert spec["source_timestamp_uncertainty_seconds"] == 0.6
    assert spec["broker_server_clock_verified"] is True


def test_symbol_spec_timestamp_stays_unknown_when_broker_clock_is_stale(monkeypatch):
    connector = _connector(monkeypatch, clock_age_seconds=30.0)

    spec = connector.get_symbol_spec("XAUUSD")

    assert spec["source_verified"] is True
    assert spec["specification_verified"] is True
    assert spec["source_timestamp_verified"] is False
    assert spec["source_timestamp_epoch"] is None
    assert spec["broker_server_clock_verified"] is False
