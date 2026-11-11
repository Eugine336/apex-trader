from collections import namedtuple
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import platforms.mt5.mt5_connector as mt5_module
from platforms.mt5.mt5_connector import MT5Connector


class FakeMT5:
    COPY_TICKS_ALL = 0
    TICK_FLAG_BUY = 16
    TICK_FLAG_SELL = 32
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TIMEFRAME_M1 = 1
    TIMEFRAME_H1 = 16385

    @staticmethod
    def symbol_info(symbol):
        return type("SymbolInfo", (), {
            "visible": True,
            "trade_tick_size": 0.01,
            "point": 0.01,
        })()

    @staticmethod
    def symbol_info_tick(symbol):
        tick_type = namedtuple(
            "Tick",
            "time time_msc bid ask last volume volume_real flags",
        )
        return tick_type(
            int(datetime.now(timezone.utc).timestamp()),
            int(datetime.now(timezone.utc).timestamp() * 1000),
            100.0, 100.02, 100.01, 3, 3.0, 16,
        )

    @staticmethod
    def symbol_select(symbol, enabled):
        return True

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        now = datetime.now(timezone.utc).timestamp()
        self.last_rates_start_pos = start
        rows = [
            {"time": int(now - 120), "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1,
             "tick_volume": 100, "real_volume": 25, "spread": 12},
            {"time": int(now - 30), "open": 1.1, "high": 1.3, "low": 1.0, "close": 1.2,
             "tick_volume": 120, "real_volume": 30, "spread": 14},
        ]
        eligible = rows if start == 0 else rows[:-start]
        return eligible[-count:]

    def copy_rates_range(self, symbol, timeframe, start, end):
        return self.copy_rates_from_pos(symbol, timeframe, 0, 2)

    def order_calc_profit(self, action, symbol, volume, price_open, price_close):
        return (price_close - price_open) * volume * (1 if action == 0 else -1) * 100

    def order_calc_margin(self, action, symbol, volume, price):
        return volume * price * 0.01

    @staticmethod
    def positions_get():
        return [SimpleNamespace(
            ticket=123,
            symbol="EURUSD",
            volume=0.2,
            price_open=1.1,
            price_current=1.2,
            profit=20.0,
            swap=-0.5,
            comment="apex-test",
        )]

    @staticmethod
    def orders_get():
        return [SimpleNamespace(
            ticket=456,
            symbol="EURUSD",
            volume_current=0.1,
            price_open=1.15,
            type=2,
            comment="apex-pending-test",
        )]

    def copy_ticks_range(self, symbol, start, end, flags):
        return [
            {"time": 1_800_000_000, "time_msc": 1_800_000_000_000, "bid": 1.0,
             "ask": 1.1, "last": 1.05, "volume": 2, "volume_real": 2.0, "flags": 16},
            {"time": 1_800_000_001, "time_msc": 1_800_000_001_000, "bid": 1.1,
             "ask": 1.2, "last": 1.15, "volume": 3, "volume_real": 3.0, "flags": 32},
        ]

    @staticmethod
    def last_error():
        return (0, "ok")


def _connected_connector(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setattr(mt5_module, "mt5", fake)
    connector = MT5Connector()
    connector._connected = True
    connector._read_server_clock_state = lambda *args, **kwargs: SimpleNamespace(
        offset_seconds=0,
        uncertainty_seconds=0.0,
        age_seconds=lambda epoch: __import__("time").time() - float(epoch),
        server_now_epoch=lambda: __import__("time").time(),
        to_utc_epoch=lambda epoch: float(epoch),
        sources=(),
    )
    return connector


def test_ohlcv_preserves_tick_volume_real_volume_and_spread(monkeypatch):
    connector = _connected_connector(monkeypatch)

    frame = connector.get_ohlcv("EURUSD", "M1", count=2)

    assert list(frame.columns) == [
        "time", "time_source_epoch", "time_server_epoch", "open", "high", "low",
        "close", "volume", "tick_volume", "real_volume", "spread",
    ]
    assert frame["volume"].tolist() == [100, 120]
    assert frame["tick_volume"].tolist() == [100, 120]
    assert frame["real_volume"].tolist() == [25, 30]
    assert frame["spread"].tolist() == [12, 14]
    assert frame.attrs["source_timestamp_verified"] is True
    assert frame.attrs["source_timezone"] == "UTC"
    assert frame.attrs["utc_normalization"] == "provider_utc_documented"
    assert frame.attrs["completed_bars_verified"] is False
    assert frame.attrs["forming_bar_included"] is True
    assert "time_server_epoch" in frame.columns
    assert frame.attrs["volume_basis"] == "tick_volume_alias_with_real_volume_preserved"


def test_ohlcv_closed_bar_view_drops_only_forming_row(monkeypatch):
    connector = _connected_connector(monkeypatch)

    frame = connector.get_ohlcv("EURUSD", "M1", count=2, include_forming=False)

    assert len(frame) == 1
    assert frame.iloc[0]["tick_volume"] == 100


def test_completed_bar_view_uses_mt5_position_one_without_broker_clock(monkeypatch):
    connector = _connected_connector(monkeypatch)
    fake = mt5_module.mt5
    connector._read_server_clock_state = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("copy_rates_from_pos position one is the closed-bar contract")
    )

    frame = connector.get_ohlcv("EURUSD", "M1", count=2, include_forming=False)

    assert fake.last_rates_start_pos == 1
    assert len(frame) == 1
    assert frame["tick_volume"].tolist() == [100]
    assert frame.attrs["completed_bars_verified"] is True
    assert frame.attrs["forming_bar_included"] is False
    assert frame.attrs["bar_completion_basis"] == "mt5_start_position_contract"


def test_completed_bar_position_is_independent_of_clock_availability(monkeypatch):
    connector = _connected_connector(monkeypatch)
    connector._read_server_clock_state = lambda *args, **kwargs: None

    frame = connector.get_ohlcv("EURUSD", "M1", count=2, include_forming=False)

    assert frame.attrs["completed_bars_verified"] is True
    assert len(frame) == 1


def test_historical_ticks_preserve_broker_fields_and_only_label_explicit_aggressor_flags(monkeypatch):
    connector = _connected_connector(monkeypatch)

    frame = connector.get_ticks(
        "EURUSD",
        datetime(2027, 1, 15, tzinfo=timezone.utc),
        datetime(2027, 1, 16, tzinfo=timezone.utc),
    )

    assert isinstance(frame, pd.DataFrame)
    assert frame["aggressor_side"].tolist() == ["buy", "sell"]
    assert frame["volume_real"].tolist() == [2.0, 3.0]
    assert frame.attrs["source"] == "mt5.copy_ticks_range"
    assert frame.attrs["source_verified"] is True


def test_history_rows_matching_utc_request_are_verified_without_live_offset(monkeypatch):
    connector = _connected_connector(monkeypatch)
    connector._read_server_clock_state = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("historical tick normalization must not consult live broker offset")
    )
    start = datetime.fromtimestamp(1_800_000_000 - 30, timezone.utc)
    end = datetime.fromtimestamp(1_800_000_001 + 30, timezone.utc)
    rows = [
        {"time": 1_800_000_000, "time_msc": 1_800_000_000_250,
         "bid": 1.0, "ask": 1.1, "flags": 16},
        {"time": 1_800_000_001, "time_msc": 1_800_000_001_750,
         "bid": 1.1, "ask": 1.2, "flags": 32},
    ]
    mt5_module.mt5.copy_ticks_range = lambda *args: rows

    frame = connector.get_ticks("EURUSD", start, end)

    assert frame.attrs["source_timezone"] == "UTC"
    assert frame.attrs["source_timestamp_verified"] is True
    assert frame.attrs["utc_normalization"] == "utc_request_bounds_verified"
    assert frame.attrs["source_utc_offset_seconds"] is None
    assert frame["time"].dt.tz is not None
    assert frame["time_msc_datetime"].dt.tz is not None
    assert frame["time_source_epoch"].tolist() == [1_800_000_000, 1_800_000_001]
    assert frame["time"].iloc[0].timestamp() == 1_800_000_000


def test_history_rows_shifted_by_broker_offset_remain_unverified(monkeypatch):
    connector = _connected_connector(monkeypatch)
    start = datetime.fromtimestamp(1_800_000_000 - 30, timezone.utc)
    end = datetime.fromtimestamp(1_800_000_000 + 30, timezone.utc)
    shifted = 1_800_000_000 + 7200
    mt5_module.mt5.copy_ticks_range = lambda *args: [
        {"time": shifted, "time_msc": shifted * 1000, "bid": 1.0, "ask": 1.1, "flags": 16}
    ]

    frame = connector.get_ticks("EURUSD", start, end)

    assert frame.attrs["source_timezone"] == "unknown"
    assert frame.attrs["source_timestamp_verified"] is False
    assert frame.attrs["utc_normalization"] == "unverified_source_epoch"
    assert frame["time"].dt.tz is None
    assert frame["time_source_epoch"].iloc[0] == shifted


def test_wide_history_range_is_not_attested_by_coincidental_bound_membership(monkeypatch):
    connector = _connected_connector(monkeypatch)
    start = datetime(2027, 1, 15, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    mt5_module.mt5.copy_ticks_range = lambda *args: [
        {"time": 1_800_000_000, "time_msc": 1_800_000_000_000,
         "bid": 1.0, "ask": 1.1, "flags": 16},
    ]

    frame = connector.get_ticks("EURUSD", start, end)

    assert frame.attrs["source_timestamp_verified"] is False
    assert frame.attrs["source_timezone"] == "unknown"
    assert frame["time"].dt.tz is None


def test_history_rows_with_inconsistent_seconds_and_milliseconds_are_unverified(monkeypatch):
    connector = _connected_connector(monkeypatch)
    start = datetime(2027, 1, 15, tzinfo=timezone.utc)
    end = datetime(2027, 1, 16, tzinfo=timezone.utc)
    mt5_module.mt5.copy_ticks_range = lambda *args: [
        {"time": 1_800_000_000, "time_msc": 1_800_000_010_000,
         "bid": 1.0, "ask": 1.1, "flags": 16}
    ]

    frame = connector.get_ticks("EURUSD", start, end)

    assert frame.attrs["source_timestamp_verified"] is False
    assert frame["time_msc_datetime"].dt.tz is None


def test_recent_tick_history_does_not_subtract_live_server_offset(monkeypatch):
    import time

    connector = _connected_connector(monkeypatch)
    now = time.time()
    first_utc_epoch = now - 10
    second_utc_epoch = now - 9
    mt5_module.mt5.copy_ticks_range = lambda *args: [
        {"time": int(first_utc_epoch), "time_msc": int(first_utc_epoch * 1000),
         "bid": 1.0, "ask": 1.1, "last": 1.05, "volume": 2,
         "volume_real": 2.0, "flags": 16},
        {"time": int(second_utc_epoch), "time_msc": int(second_utc_epoch * 1000),
         "bid": 1.1, "ask": 1.2, "last": 1.15, "volume": 3,
         "volume_real": 3.0, "flags": 32},
    ]
    connector._read_server_clock_state = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("historical tick normalization must not consult live broker offset")
    )
    end = datetime.fromtimestamp(now, timezone.utc)
    frame = connector.get_ticks("EURUSD", end - timedelta(seconds=10), end)

    assert frame.attrs["source_timezone"] == "UTC"
    assert frame.attrs["source_timestamp_verified"] is True
    assert frame.attrs["source_utc_offset_seconds"] is None
    assert abs(frame.iloc[0]["time"].timestamp() - first_utc_epoch) < 1.0
    assert frame["time_source_epoch"].iloc[0] == int(first_utc_epoch)


def test_historical_ticks_reject_naive_datetimes(monkeypatch):
    connector = _connected_connector(monkeypatch)

    try:
        connector.get_ticks("EURUSD", datetime(2027, 1, 15), datetime(2027, 1, 16))
    except ValueError as exc:
        assert "timezone-aware" in str(exc)
    else:
        raise AssertionError("naive datetimes must be rejected")


def test_historical_bar_range_preserves_native_fields(monkeypatch):
    import time

    connector = _connected_connector(monkeypatch)
    now = time.time()
    frame = connector.get_ohlcv_range(
        "EURUSD",
        "M1",
        datetime.fromtimestamp(now - 180, timezone.utc),
        datetime.fromtimestamp(now + 10, timezone.utc),
        include_forming=True,
    )
    assert frame["tick_volume"].tolist() == [100, 120]
    assert frame["real_volume"].tolist() == [25, 30]
    assert frame["spread"].tolist() == [12, 14]
    assert frame.attrs["source"] == "mt5.copy_rates_range"
    assert frame.attrs["source_timezone"] == "UTC"
    assert frame.attrs["utc_normalization"] == "provider_utc_documented"
    assert frame.attrs["completed_bars_verified"] is False


def test_historical_range_completed_filter_uses_independent_utc_lower_bound(monkeypatch):
    import time

    connector = _connected_connector(monkeypatch)
    now = time.time()
    connector.get_utc_time_lower_bound = lambda: now - 0.5
    frame = connector.get_ohlcv_range(
        "EURUSD", "M1",
        datetime.fromtimestamp(now - 180, timezone.utc),
        datetime.fromtimestamp(now + 10, timezone.utc),
        include_forming=False,
    )

    assert len(frame) == 1
    assert frame["tick_volume"].tolist() == [100]
    assert frame.attrs["completed_bars_verified"] is True
    assert frame.attrs["source_timezone"] == "UTC"


def test_broker_profit_and_margin_calculations_use_native_mt5_functions(monkeypatch):
    connector = _connected_connector(monkeypatch)
    profit = connector.calculate_profit("EURUSD", "BUY", 0.2, 1.0, 1.1)
    margin = connector.calculate_margin("EURUSD", "BUY", 0.2, 1.1)
    assert profit == pytest.approx(2.0)
    assert margin == pytest.approx(0.0022)


def test_unregistered_broker_symbol_quote_preserves_raw_tick_and_uses_tick_units(monkeypatch):
    connector = _connected_connector(monkeypatch)

    tick = connector.get_price("BROKER_CUSTOM")

    assert tick.spread == pytest.approx(2.0)
    assert tick.spread_unit == "ticks"
    assert tick.raw_fields["flags"] == 16
    assert tick.raw_fields["volume"] == 3
    assert tick.source_timestamp_verified is True


def test_server_clock_tick_is_normalized_to_utc_with_raw_provenance(monkeypatch):
    import time

    connector = _connected_connector(monkeypatch)
    now = time.time()
    offset = 7200
    server_epoch = now + offset
    fake_tick = SimpleNamespace(
        time=int(server_epoch),
        time_msc=int(server_epoch * 1000),
        bid=100.0, ask=100.02, last=100.01,
        volume=3, volume_real=3.0, flags=16,
    )
    mt5_module.mt5.symbol_info_tick = lambda _symbol: fake_tick
    connector._read_server_clock_state = lambda *args, **kwargs: SimpleNamespace(
        offset_seconds=offset,
        uncertainty_seconds=0.0,
        age_seconds=lambda epoch: (now + offset) - float(epoch),
        to_utc_epoch=lambda epoch: float(epoch) - offset,
        sources=(),
    )

    tick = connector.get_price("BROKER_CUSTOM")

    assert abs(tick.time.timestamp() - now) < 1.0
    assert tick.raw_fields["time"] == int(server_epoch)
    assert tick.raw_fields["source_timezone"] == "broker_server"
    assert tick.raw_fields["source_utc_offset_seconds"] == 7200
    assert abs(tick.raw_fields["timestamp_utc_epoch"] - now) < 1.0


def test_raw_position_and_pending_order_snapshots_preserve_broker_fields(monkeypatch):
    connector = _connected_connector(monkeypatch)

    positions = connector.get_position_snapshots()
    orders = connector.get_pending_order_snapshots()

    assert positions[0]["ticket"] == 123
    assert positions[0]["swap"] == -0.5
    assert positions[0]["source"] == "mt5.positions_get"
    assert orders[0]["ticket"] == 456
    assert orders[0]["type"] == 2
    assert orders[0]["source"] == "mt5.orders_get"


def test_stale_live_clock_is_not_applied_to_historical_tick_window(monkeypatch):
    import time

    connector = _connected_connector(monkeypatch)
    now = time.time()
    offset = 7200
    source_epoch = now - 3600 + offset
    mt5_module.mt5.copy_ticks_range = lambda *args: [
        {"time": int(source_epoch), "time_msc": int(source_epoch * 1000),
         "bid": 1.0, "ask": 1.1, "last": 1.05, "volume": 2,
         "volume_real": 2.0, "flags": 16},
    ]
    connector._read_server_clock_state = lambda *args, **kwargs: SimpleNamespace(
        offset_seconds=offset,
        uncertainty_seconds=0.0,
        observed_gmt_epoch=now,
        observed_monotonic=time.monotonic(),
        sources=("ntp:one", "ntp:two"),
    )
    end = datetime.fromtimestamp(now - 3600, timezone.utc)
    frame = connector.get_ticks("EURUSD", end - timedelta(seconds=10), end)

    assert frame.attrs["source_timezone"] == "unknown"
    assert frame.attrs["source_timestamp_verified"] is False
    assert frame.attrs["utc_normalization"] == "unverified_source_epoch"
    assert frame["time_msc_datetime"].dt.tz is None

def test_historical_range_completed_filter_fails_closed_without_independent_utc(monkeypatch):
    import time
    import pytest

    connector = _connected_connector(monkeypatch)
    now = time.time()
    connector.get_utc_time_lower_bound = lambda: None

    with pytest.raises(RuntimeError, match="Independent UTC authority unavailable"):
        connector.get_ohlcv_range(
            "EURUSD", "M1",
            datetime.fromtimestamp(now - 180, timezone.utc),
            datetime.fromtimestamp(now + 10, timezone.utc),
            include_forming=False,
        )
