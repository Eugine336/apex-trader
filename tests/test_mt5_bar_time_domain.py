"""WP-T: bar timestamps are proven UTC (or normalized by a proven offset) or marked unverified.

Without this a broker whose server clock is not UTC produces bars labelled hours in the
future/past, every concept reading fails its freshness check and no geometry is ever built.
"""

import importlib.util
import pathlib
import time
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location(
    "_mt5_market_data_helpers", pathlib.Path(__file__).with_name("test_mt5_market_data.py"),
)
_h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_h)
mt5_module = _h.mt5_module
_connected_connector = _h._connected_connector

_ci_spec = importlib.util.spec_from_file_location(
    "_concept_integration_helpers", pathlib.Path(__file__).with_name("test_concept_integration.py"),
)
_ci = importlib.util.module_from_spec(_ci_spec)
_ci_spec.loader.exec_module(_ci)


def _clock(offset, now, uncertainty=0.0):
    server_now = now + offset
    return SimpleNamespace(
        offset_seconds=offset, uncertainty_seconds=uncertainty, server_now_epoch=lambda: server_now,
        to_utc_epoch=lambda epoch: float(epoch) - offset, age_seconds=lambda epoch: server_now - float(epoch),
        sources=("ntp:one", "ntp:two"),
    )


def _m1_rows(label_now):
    return [
        {"time": int(label_now - 90), "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1,
         "tick_volume": 10, "real_volume": 0, "spread": 10},
        {"time": int(label_now - 30), "open": 1.1, "high": 1.3, "low": 1.0, "close": 1.2,
         "tick_volume": 11, "real_volume": 0, "spread": 10},
    ]


def test_server_offset_labels_are_proven_and_removed():
    now = time.time()
    connector = _connected_connector(pytest.MonkeyPatch())
    connector._read_server_clock_state = lambda *a, **k: _clock(7200, now)
    mt5_module.mt5.copy_rates_from_pos = lambda *a: _m1_rows(now + 7200)
    frame = connector.get_ohlcv("EURUSD", "M1", count=2, include_forming=True)
    assert frame.attrs["utc_normalization"] == "m1_open_proof_server_offset"
    assert frame.attrs["source_timestamp_verified"] is True
    assert abs(frame["time"].iloc[-1].timestamp() - (now - 30)) < 2.0       # now a true UTC time
    assert "bars before a broker daylight-saving change" in frame.attrs["offset_validity"]


def test_utc_labels_with_a_nonzero_server_offset_are_recognized_as_utc():
    now = time.time()
    connector = _connected_connector(pytest.MonkeyPatch())
    connector._read_server_clock_state = lambda *a, **k: _clock(7200, now)     # server runs ahead of UTC
    mt5_module.mt5.copy_rates_from_pos = lambda *a: _m1_rows(now)             # but bars are labelled in UTC
    frame = connector.get_ohlcv("EURUSD", "M1", count=2, include_forming=True)
    assert frame.attrs["utc_normalization"] == "m1_open_proof_utc"
    assert frame.attrs["source_utc_offset_seconds"] == 0.0
    assert abs(frame["time"].iloc[-1].timestamp() - (now - 30)) < 2.0


def test_labels_that_fit_neither_hypothesis_stay_unverified():
    now = time.time()
    connector = _connected_connector(pytest.MonkeyPatch())
    connector._read_server_clock_state = lambda *a, **k: _clock(7200, now)
    mt5_module.mt5.copy_rates_from_pos = lambda *a: _m1_rows(now + 3600)      # +1 h: neither UTC nor +2 h
    frame = connector.get_ohlcv("EURUSD", "M1", count=2, include_forming=True)
    assert frame.attrs["source_timestamp_verified"] is False
    assert frame.attrs["utc_normalization"] == "unresolved"
    assert frame.attrs["source_timezone"] == "broker_server"
    assert "neither" in frame.attrs["bar_time_evidence"]["reason"]


def test_no_broker_clock_means_unverified_bars():
    now = time.time()
    connector = _connected_connector(pytest.MonkeyPatch())
    connector._read_server_clock_state = lambda *a, **k: None
    mt5_module.mt5.copy_rates_from_pos = lambda *a: _m1_rows(now)
    frame = connector.get_ohlcv("EURUSD", "M1", count=2, include_forming=True)
    assert frame.attrs["source_timestamp_verified"] is False
    assert frame.attrs["bar_time_evidence"]["reason"] == "broker clock unavailable"


def test_unverified_bars_produce_no_concept_geometry_and_proven_offset_bars_do():
    """End to end on the live-shaped path: broker bars -> lenses -> concepts -> geometry."""
    from cognition.market_lenses import analyze_market_frames
    from cognition.contracts import MarketState
    from cognition.opportunity_geometry import derive_opportunity_geometry

    def geometry(offset, clock_offset):
        now = time.time()
        crt = _ci._crt_frame()                                   # bullish CRT fixture, UTC-labelled
        rows = [
            {"time": int(r.time + offset), "open": r.open, "high": r.high, "low": r.low, "close": r.close,
             "tick_volume": int(r.volume), "real_volume": 0, "spread": 10}
            for r in crt.itertuples()
        ]
        connector = _connected_connector(pytest.MonkeyPatch())
        connector._read_server_clock_state = lambda *a, **k: _clock(clock_offset, now) if clock_offset is not None else None
        # M1 proof bars follow the broker's labelling; H1 returns the fixture bars
        def rates(symbol, timeframe, start, count):
            # the proof asks for the two newest M1 bars; the analysis asks for the whole H1 history
            return rows if count > 2 else _m1_rows(now + offset)
        mt5_module.mt5.copy_rates_from_pos = rates
        frame = connector.get_ohlcv("EURUSD", "H1", count=len(rows), include_forming=True)
        state = MarketState(symbol="EURUSD")
        for ev in analyze_market_frames("EURUSD", {"H1": frame}):
            state.add(ev)
        return derive_opportunity_geometry(state, "LONG")

    assert geometry(offset=7200, clock_offset=7200) is not None        # proven +2 h labels -> usable
    assert geometry(offset=0, clock_offset=0) is not None               # UTC broker
    assert geometry(offset=7200, clock_offset=None) is None             # unproven -> unknown, never guessed
