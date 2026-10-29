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


def _frame(start, closes, *, naive=False):
    rows = []
    for index, close in enumerate(closes):
        stamp = start + timedelta(hours=index)
        if naive:
            stamp = stamp.replace(tzinfo=None)
        rows.append({"time": stamp, "close": float(close)})
    return pd.DataFrame(rows)


def _system(related_frame):
    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._ctx = SimpleNamespace(correlation_engine=_CorrelationEngine())
    system._pm = _FakePlatformManager(related_frame)
    return system


def test_cross_asset_evidence_uses_exact_common_closed_utc_bars():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    xau = _frame(start, [2000 + i * (1 + (i % 3) * 0.1) for i in range(32)])
    # A related leg with the same UTC timestamps and a non-constant return series.
    usd_jpy = _frame(start, [140 + i * (0.2 + (i % 4) * 0.03) for i in range(32)])
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
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    xau = _frame(start, [2000 + i for i in range(32)])
    usd_jpy = _frame(start, [140 + i * (1 + (i % 2) * 0.1) for i in range(32)], naive=True)
    assert _system(usd_jpy)._cognition_cross_asset_evidence("XAUUSD", {"H1": xau}) == []


def test_cross_asset_evidence_rejects_insufficient_common_timestamps():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    xau = _frame(start, [2000 + i * (1 + (i % 3) * 0.1) for i in range(32)])
    usd_jpy = _frame(start + timedelta(hours=15), [140 + i * (0.2 + (i % 4) * 0.03) for i in range(32)])
    assert _system(usd_jpy)._cognition_cross_asset_evidence("XAUUSD", {"H1": xau}) == []


def _cost_system(spread_getter, *, commission_verified=False):
    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._pm = SimpleNamespace(get_spread=spread_getter)
    system._config = SimpleNamespace(
        cognition=SimpleNamespace(
            commission_per_lot_round_trip=0.0,
            commission_model_verified=commission_verified,
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
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    xau = _frame(start, [2000 + i * (1 + (i % 3) * 0.1) for i in range(32)])
    usd_jpy = _frame(start, [140 + i * (0.2 + (i % 4) * 0.03) for i in range(32)])
    evidence = _system(usd_jpy)._cognition_cross_asset_evidence("XAUUSD", {"H1": xau})
    result = analyze_market_context(evidence, "XAUUSD")
    relationships = result["cross_market_relationships"]
    assert relationships["cross_asset_status"] == "verified_synchronized_observations_present"
    assert relationships["cross_asset_records"][0]["causation_established"] is False
    assert "synchronized cross-asset relationship measurements with a defined window and source provenance" not in result["missing_information"]


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
