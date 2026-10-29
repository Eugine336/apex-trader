"""Broker-bar cross-asset evidence must be synchronized and descriptive only."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd

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
