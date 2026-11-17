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
        return [
            {"time": int(now - 120), "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1,
             "tick_volume": 100, "real_volume": 25, "spread": 12},
            {"time": int(now - 30), "open": 1.1, "high": 1.3, "low": 1.0, "close": 1.2,
             "tick_volume": 120, "real_volume": 30, "spread": 14},
        ]

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
        "time", "time_server_epoch", "open", "high", "low", "close", "volume",
        "tick_volume", "real_volume", "spread",
    ]
    assert frame["volume"].tolist() == [100, 120]
    assert frame["tick_volume"].tolist() == [100, 120]
    assert frame["real_volume"].tolist() == [25, 30]
    assert frame["spread"].tolist() == [12, 14]
    assert frame.attrs["source_timestamp_verified"] is True
    assert frame.attrs["source_timezone"] == "UTC"
    assert frame.attrs["utc_normalization"] == "m1_open_proof_utc"
    assert frame.attrs["source_utc_offset_seconds"] == 0.0
    assert "time_server_epoch" in frame.columns
    assert frame.attrs["volume_basis"] == "tick_volume_alias_with_real_volume_preserved"


def test_ohlcv_closed_bar_view_drops_only_forming_row(monkeypatch):
    connector = _connected_connector(monkeypatch)

    frame = connector.get_ohlcv("EURUSD", "M1", count=2, include_forming=False)

    assert len(frame) == 1
    assert frame.iloc[0]["tick_volume"] == 100


def test_completed_bar_filter_compares_in_the_proven_domain_not_across_domains(monkeypatch):
    import time

    connector = _connected_connector(monkeypatch)
    now = time.time()
    offset = 7200
    server_now = now + offset
    mt5_module.mt5.copy_rates_from_pos = lambda *args: [
        {"time": int(server_now - 120), "open": 1.0, "high": 1.2, "low": 0.9,
         "close": 1.1, "tick_volume": 10, "real_volume": 0, "spread": 10},
        {"time": int(server_now - 30), "open": 1.1, "high": 1.3, "low": 1.0,
         "close": 1.2, "tick_volume": 11, "real_volume": 0, "spread": 10},
    ]
    connector._read_server_clock_state = lambda *args, **kwargs: SimpleNamespace(
        offset_seconds=offset,
        uncertainty_seconds=0.0,
        age_seconds=lambda epoch: server_now - float(epoch),
        server_now_epoch=lambda: server_now,
        to_utc_epoch=lambda epoch: float(epoch) - offset,
        sources=(),
    )

    frame = connector.get_ohlcv("EURUSD", "M1", count=2, include_forming=False)

    # Broker labels are +7200 s: proven from the newest M1 bar, removed from the time column,
    # and the completed-bar filter compares in that same UTC domain.
    assert frame["time_server_epoch"].tolist() == [int(server_now - 120)]
    assert frame.attrs["source_timezone"] == "UTC"
    assert frame.attrs["utc_normalization"] == "m1_open_proof_server_offset"
    assert frame.attrs["source_utc_offset_seconds"] == offset
    assert frame["time"].iloc[0].timestamp() == int(server_now - 120) - offset


def test_recent_completed_bar_read_fails_closed_without_server_clock(monkeypatch):
    connector = _connected_connector(monkeypatch)
    connector._read_server_clock_state = lambda *args, **kwargs: None

    with pytest.raises(RuntimeError, match="completed-bar status cannot be verified"):
        connector.get_ohlcv("EURUSD", "M1", count=2, include_forming=False)


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


def test_historical_ticks_inside_the_utc_request_bounds_are_verified_utc(monkeypatch):
    connector = _connected_connector(monkeypatch)
    connector._read_server_clock_state = lambda *args, **kwargs: None      # UTC proof needs no clock
    frame = connector.get_ticks(
        "EURUSD",
        datetime(2027, 1, 15, tzinfo=timezone.utc),
        datetime(2027, 1, 16, tzinfo=timezone.utc),
    )

    assert frame.attrs["source_timezone"] == "UTC"
    assert frame.attrs["source_timestamp_verified"] is True
    assert frame.attrs["utc_normalization"] == "request_bounds_utc"
    assert frame.attrs["source_utc_offset_seconds"] == 0.0
    assert frame.attrs["time_domain_evidence"]["rows_inside_utc_request_bounds"] is True
    # raw provider epochs are preserved exactly and the UTC label is not shifted
    assert frame["time_msc_raw_epoch"].tolist() == [1_800_000_000_000, 1_800_000_001_000]
    assert frame["time_msc_datetime"].iloc[0].timestamp() == 1_800_000_000.0
    assert frame["time_msc_datetime"].dt.tz is not None


def test_owner_host_pattern_history_is_utc_even_when_the_latest_quote_clock_is_server_time(monkeypatch):
    # Owner-host evidence: latest-quote epochs run ~+7200 s ahead of UTC (broker server
    # time) while tick-history rows sit inside the UTC request bounds. Subtracting the
    # +7200 offset from history would push every row two hours outside the window.
    connector = _connected_connector(monkeypatch)
    connector._read_server_clock_state = lambda *args, **kwargs: SimpleNamespace(
        offset_seconds=7200, uncertainty_seconds=0.0, sources=("ntp:one", "ntp:two"),
    )
    # The poller asks for short overlapping windows; use the same shape here.
    frame = connector.get_ticks(
        "EURUSD",
        datetime.fromtimestamp(1_799_999_990, timezone.utc),
        datetime.fromtimestamp(1_800_000_010, timezone.utc),
    )

    assert frame.attrs["utc_normalization"] == "request_bounds_utc"
    assert frame.attrs["source_timestamp_verified"] is True
    assert frame["time_msc_datetime"].iloc[0].timestamp() == 1_800_000_000.0   # not 1_799_992_800.0
    ev = frame.attrs["time_domain_evidence"]
    assert ev["rows_inside_utc_request_bounds"] is True and ev["rows_inside_offset_shifted_bounds"] is False


def test_a_window_wider_than_the_offset_cannot_prove_a_domain_and_stays_unresolved(monkeypatch):
    # With a 24 h window and a 2 h offset the same rows fit both hypotheses, so the
    # evidence cannot choose; the rows stay unverified instead of being guessed.
    connector = _connected_connector(monkeypatch)
    connector._read_server_clock_state = lambda *args, **kwargs: SimpleNamespace(
        offset_seconds=7200, uncertainty_seconds=0.0, sources=("ntp:one", "ntp:two"),
    )
    frame = connector.get_ticks(
        "EURUSD",
        datetime(2027, 1, 15, tzinfo=timezone.utc),
        datetime(2027, 1, 16, tzinfo=timezone.utc),
    )
    assert frame.attrs["utc_normalization"] == "unresolved"
    assert frame.attrs["source_timestamp_verified"] is False


def test_history_inside_the_offset_shifted_bounds_is_normalized_by_that_proven_offset(monkeypatch):
    connector = _connected_connector(monkeypatch)
    offset = 7200
    start = datetime(2027, 1, 15, tzinfo=timezone.utc)
    end = datetime(2027, 1, 15, 0, 1, tzinfo=timezone.utc)
    base = int(start.timestamp()) + offset + 5          # broker-server labels inside [start+off, end+off]
    mt5_module.mt5.copy_ticks_range = lambda *args: [
        {"time": base, "time_msc": base * 1000, "bid": 1.0, "ask": 1.1, "last": 0.0, "volume": 0,
         "volume_real": 0.0, "flags": 6},
    ]
    connector._read_server_clock_state = lambda *args, **kwargs: SimpleNamespace(
        offset_seconds=offset, uncertainty_seconds=0.0, sources=("ntp:one", "ntp:two"),
    )
    frame = connector.get_ticks("EURUSD", start, end)

    assert frame.attrs["utc_normalization"] == "request_bounds_server_offset"
    assert frame.attrs["source_timestamp_verified"] is True
    assert frame.attrs["source_utc_offset_seconds"] == offset
    assert frame["time_msc_datetime"].iloc[0].timestamp() == base - offset
    assert frame["time_msc_raw_epoch"].iloc[0] == base * 1000


def test_ambiguous_or_contradictory_history_stays_unverified_and_unconverted(monkeypatch):
    connector = _connected_connector(monkeypatch)
    start = datetime(2027, 1, 15, tzinfo=timezone.utc)
    end = datetime(2027, 1, 15, 0, 1, tzinfo=timezone.utc)
    inside = int(start.timestamp()) + 5
    outside = int(start.timestamp()) + 3600           # inside neither hypothesis
    mt5_module.mt5.copy_ticks_range = lambda *args: [
        {"time": inside, "time_msc": inside * 1000, "bid": 1.0, "ask": 1.1, "last": 0.0, "volume": 0,
         "volume_real": 0.0, "flags": 6},
        {"time": outside, "time_msc": outside * 1000, "bid": 1.0, "ask": 1.1, "last": 0.0, "volume": 0,
         "volume_real": 0.0, "flags": 6},
    ]
    connector._read_server_clock_state = lambda *args, **kwargs: SimpleNamespace(
        offset_seconds=7200, uncertainty_seconds=0.0, sources=("ntp:one", "ntp:two"),
    )
    frame = connector.get_ticks("EURUSD", start, end)

    assert frame.attrs["source_timestamp_verified"] is False
    assert frame.attrs["utc_normalization"] == "unresolved"
    assert frame.attrs["source_timezone"] == "broker_server"
    assert frame["time_msc_datetime"].dt.tz is None            # never labelled UTC
    assert frame["time_msc_raw_epoch"].tolist() == [inside * 1000, outside * 1000]


def test_recent_tick_window_uses_verified_live_server_offset(monkeypatch):
    import time

    connector = _connected_connector(monkeypatch)
    now = time.time()
    offset = 7200
    first_server_epoch = now + offset - 10
    second_server_epoch = now + offset - 9
    mt5_module.mt5.copy_ticks_range = lambda *args: [
        {"time": int(first_server_epoch), "time_msc": int(first_server_epoch * 1000),
         "bid": 1.0, "ask": 1.1, "last": 1.05, "volume": 2,
         "volume_real": 2.0, "flags": 16},
        {"time": int(second_server_epoch), "time_msc": int(second_server_epoch * 1000),
         "bid": 1.1, "ask": 1.2, "last": 1.15, "volume": 3,
         "volume_real": 3.0, "flags": 32},
    ]
    connector._read_server_clock_state = lambda *args, **kwargs: SimpleNamespace(
        offset_seconds=offset,
        uncertainty_seconds=0.0,
        age_seconds=lambda epoch: (now + offset) - float(epoch),
        server_now_epoch=lambda: now + offset,
        observed_gmt_epoch=now,
        observed_monotonic=__import__("time").monotonic(),
        to_utc_epoch=lambda epoch: float(epoch) - offset,
        sources=(),
    )
    end = datetime.fromtimestamp(now, timezone.utc)
    frame = connector.get_ticks("EURUSD", end - timedelta(seconds=10), end)

    assert frame.attrs["source_timezone"] == "UTC"
    assert frame.attrs["source_timestamp_verified"] is True
    assert frame.attrs["source_utc_offset_seconds"] == offset
    assert abs(frame.iloc[0]["time"].timestamp() - (first_server_epoch - offset)) < 1.0
    assert frame.iloc[0]["time_server_epoch"] == int(first_server_epoch)


def test_historical_ticks_reject_naive_datetimes(monkeypatch):
    connector = _connected_connector(monkeypatch)

    try:
        connector.get_ticks("EURUSD", datetime(2027, 1, 15), datetime(2027, 1, 16))
    except ValueError as exc:
        assert "timezone-aware" in str(exc)
    else:
        raise AssertionError("naive datetimes must be rejected")


def test_historical_bar_range_preserves_native_fields(monkeypatch):
    connector = _connected_connector(monkeypatch)
    frame = connector.get_ohlcv_range(
        "EURUSD",
        "M1",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 2, tzinfo=timezone.utc),
        include_forming=True,
    )
    assert frame["tick_volume"].tolist() == [100, 120]
    assert frame["real_volume"].tolist() == [25, 30]
    assert frame["spread"].tolist() == [12, 14]
    assert frame.attrs["source"] == "mt5.copy_rates_range"
    assert frame.attrs["source_timezone"] == "UTC"
    assert frame.attrs["utc_normalization"] == "m1_open_proof_utc"


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


def test_a_clock_offset_that_the_data_does_not_confirm_is_never_applied(monkeypatch):
    # e.g. a DST change inside the window: today's offset (7200) does not describe rows
    # that were labelled with last season's offset (3600).
    import time

    connector = _connected_connector(monkeypatch)
    now = time.time()
    source_epoch = now - 3600 + 3600                     # labelled with a 3600 s offset
    mt5_module.mt5.copy_ticks_range = lambda *args: [
        {"time": int(source_epoch), "time_msc": int(source_epoch * 1000),
         "bid": 1.0, "ask": 1.1, "last": 1.05, "volume": 2, "volume_real": 2.0, "flags": 16},
    ]
    connector._read_server_clock_state = lambda *args, **kwargs: SimpleNamespace(
        offset_seconds=7200, uncertainty_seconds=0.0, observed_gmt_epoch=now,
        observed_monotonic=time.monotonic(), sources=("ntp:one", "ntp:two"),
    )
    end = datetime.fromtimestamp(now - 3600, timezone.utc)
    frame = connector.get_ticks("EURUSD", end - timedelta(seconds=10), end)

    assert frame.attrs["source_timestamp_verified"] is False
    assert frame.attrs["utc_normalization"] == "unresolved"
    assert frame["time_msc_datetime"].dt.tz is None
