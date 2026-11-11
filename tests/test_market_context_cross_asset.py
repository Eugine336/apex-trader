"""Broker-bar cross-asset evidence must be synchronized and descriptive only."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd

from cognition.market_context import analyze_market_context
from event_driven_bootstrap import EventDrivenSystem


class _CorrelationEngine:
    def inverse_correlates(self, symbol):
        return ("USDJPY",) if symbol == "XAUUSD" else ()


class _FakePlatformManager:
    def __init__(self, related_frame):
        self.related_frame = related_frame

    def fetch_market_data(self, symbol, timeframes, count=64, include_forming=False):
        assert symbol == "USDJPY"
        assert timeframes == ["H1"]
        assert include_forming is False
        return {"H1": self.related_frame}


def _frame(start, closes, *, symbol, naive=False):
    rows = []
    for index, close in enumerate(closes):
        stamp = start + timedelta(hours=index)
        if naive:
            stamp = stamp.replace(tzinfo=None)
        rows.append({"time": stamp, "close": float(close)})
    frame = pd.DataFrame(rows)
    frame.attrs.update({
        "source_timestamp_verified": True,
        "source_symbol": symbol,
        "source_timestamp_unit": "unix_seconds",
        "source_timestamp_origin": "broker_ohlcv",
        "source_broker": "test-broker",
        "source_timezone": "UTC",
        "completed_bars_verified": True,
        "forming_bar_included": False,
    })
    return frame


def _system(related_frame):
    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._ctx = SimpleNamespace(correlation_engine=_CorrelationEngine())
    system._pm = _FakePlatformManager(related_frame)
    return system


def test_cross_asset_evidence_uses_exact_common_closed_utc_bars():
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=31)
    xau = _frame(start, [2000 + i * (1 + (i % 3) * 0.1) for i in range(32)], symbol="XAUUSD")
    # A related leg with the same UTC timestamps and a non-constant return series.
    usd_jpy = _frame(start - timedelta(hours=1), [140 + i * (0.2 + (i % 4) * 0.03) for i in range(32)], symbol="USDJPY")
    result = _system(usd_jpy)._cognition_cross_asset_evidence("XAUUSD", {"H1": xau})
    assert len(result) == 1
    evidence = result[0]
    assert evidence.measurements["related_symbol"] == "USDJPY"
    assert evidence.measurements["synchronized_timestamps"] is True
    assert evidence.measurements["source_verified"] is True
    assert evidence.measurements["sample_count"] >= 20
    assert -1.0 <= evidence.measurements["correlation_coefficient"] <= 1.0
    assert evidence.measurements["causation_established"] is False


def test_cross_asset_evidence_rejects_naive_timestamps():
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=31)
    xau = _frame(start, [2000 + i for i in range(32)], symbol="XAUUSD")
    usd_jpy = _frame(start - timedelta(hours=1), [140 + i * (1 + (i % 2) * 0.1) for i in range(32)], symbol="USDJPY", naive=True)
    assert _system(usd_jpy)._cognition_cross_asset_evidence("XAUUSD", {"H1": xau}) == []


def test_cross_asset_evidence_rejects_insufficient_common_timestamps():
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=31)
    xau = _frame(start, [2000 + i * (1 + (i % 3) * 0.1) for i in range(32)], symbol="XAUUSD")
    usd_jpy = _frame(start + timedelta(hours=14), [140 + i * (0.2 + (i % 4) * 0.03) for i in range(32)], symbol="USDJPY")
    assert _system(usd_jpy)._cognition_cross_asset_evidence("XAUUSD", {"H1": xau}) == []


def _cost_system(spread_getter, *, commission_verified=False):
    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._pm = SimpleNamespace(get_spread=spread_getter)
    system._config = SimpleNamespace(
        cognition=SimpleNamespace(
            commission_per_lot_round_trip=0.0,
            commission_model_verified=commission_verified,
            financing_per_lot_round_trip={"XAUUSD": 0.0},
            financing_model_verified=True,
        )
    )
    system._money_per_price = lambda symbol, lots: 100.0
    system._safe_pip_size = lambda symbol: 0.01
    system._commission_per_lot_est = 0.0
    return system


def test_round_trip_cost_requires_positive_observed_spread():
    system = _cost_system(lambda symbol: 2.0, commission_verified=True)
    assert system._estimate_round_trip_cost("XAUUSD", 1.0) == 3.0


def test_round_trip_cost_fails_closed_when_spread_is_zero_or_unknown():
    assert _cost_system(lambda symbol: 0.0)._estimate_round_trip_cost("XAUUSD", 1.0) == 0.0

    def unavailable(symbol):
        raise RuntimeError("quote unavailable")

    assert _cost_system(unavailable)._estimate_round_trip_cost("XAUUSD", 1.0) == 0.0


def test_measured_cross_asset_evidence_is_classified_as_verified_association():
    # The final timestamp is the open time of the last completed H1 bar;
    # keep it one full hour behind the current UTC boundary.
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=32)
    xau = _frame(start, [2000 + i * (1 + (i % 3) * 0.1) for i in range(32)], symbol="XAUUSD")
    usd_jpy = _frame(start, [140 + i * (0.2 + (i % 4) * 0.03) for i in range(32)], symbol="USDJPY")
    evidence = _system(usd_jpy)._cognition_cross_asset_evidence("XAUUSD", {"H1": xau})
    result = analyze_market_context(evidence, "XAUUSD")
    relationships = result["cross_market_relationships"]
    assert relationships["cross_asset_status"] == "verified_synchronized_observations_present"
    assert relationships["cross_asset_records"][0]["causation_established"] is False
    assert "synchronized cross-asset relationship measurements with a defined window and source provenance" not in result["missing_information"]


def test_round_trip_cost_fails_closed_when_financing_model_is_unknown():
    system = _cost_system(lambda symbol: 2.0, commission_verified=True)
    system._config.cognition.financing_model_verified = False
    assert system._estimate_round_trip_cost("XAUUSD", 1.0) == 0.0

    system = _cost_system(lambda symbol: 2.0, commission_verified=True)
    system._config.cognition.financing_per_lot_round_trip = {}
    assert system._estimate_round_trip_cost("XAUUSD", 1.0) == 0.0


def test_round_trip_cost_fails_closed_when_zero_commission_is_unverified():
    system = _cost_system(lambda symbol: 2.0, commission_verified=False)
    assert system._estimate_round_trip_cost("XAUUSD", 1.0) == 0.0



def test_money_per_price_requires_explicit_verified_broker_spec():
    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._pm = SimpleNamespace(get_symbol_spec=lambda symbol: {
        "source_verified": True,
        "specification_verified": True,
        "trade_tick_value": 1.0,
        "trade_tick_size": 0.01,
    })
    assert system._money_per_price("XAUUSD", 2.0) == 200.0


def test_money_per_price_rejects_unverified_or_invalid_broker_spec():
    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._pm = SimpleNamespace(get_symbol_spec=lambda symbol: {
        "source_verified": True,
        "specification_verified": False,
        "trade_tick_value": 1.0,
        "trade_tick_size": 0.01,
    })
    assert system._money_per_price("XAUUSD", 2.0) == 0.0

    system._pm = SimpleNamespace(get_symbol_spec=lambda symbol: {
        "source_verified": True,
        "specification_verified": True,
        "trade_tick_value": float("nan"),
        "trade_tick_size": 0.01,
    })
    assert system._money_per_price("XAUUSD", 2.0) == 0.0



def test_confirmed_candle_fetcher_requests_and_requires_completed_bars():
    start = datetime.now(timezone.utc) - timedelta(hours=3)
    completed = _frame(start, [2000 + index for index in range(32)], symbol="XAUUSD")

    class _CompletedBarsPM:
        def __init__(self, frame):
            self.frame = frame
            self.include_forming_values = []

        def fetch_market_data(self, symbol, timeframes, count, *, include_forming=True):
            self.include_forming_values.append(include_forming)
            return {timeframes[0]: self.frame}

    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._pm = _CompletedBarsPM(completed)

    assert system._fetch_candles("XAUUSD", "H1", 64) is completed
    assert system._pm.include_forming_values == [False]

    completed.attrs["completed_bars_verified"] = False
    assert system._fetch_candles("XAUUSD", "H1", 64) is None
    assert system._pm.include_forming_values == [False, False]
