"""Tests for cognition.market_snapshot — the Brain's reconstructed live chart."""

from types import SimpleNamespace

from cognition.market_snapshot import build_price_snapshot, snapshot_to_evidence


def _uptrend(n=8, start=100.0, step=0.5):
    rows = []
    p = start
    for _ in range(n):
        o = p
        c = p + step
        rows.append((o, c + 0.1, o - 0.1, c))
        p = c
    return rows


def _downtrend(n=8, start=100.0, step=0.5):
    rows = []
    p = start
    for _ in range(n):
        o = p
        c = p - step
        rows.append((o, o + 0.1, c - 0.1, c))
        p = c
    return rows


def test_snapshot_shapes_and_trend():
    snap = build_price_snapshot(
        "XAUUSD",
        {"M5": _uptrend(), "M15": _uptrend(start=90.0)},
        tick=SimpleNamespace(bid=104.0, ask=104.3, mid=104.15),
    )
    assert snap["symbol"] == "XAUUSD"
    assert set(snap["timeframes"].keys()) == {"M5", "M15"}
    m5 = snap["timeframes"]["M5"]
    assert m5["trend"] == "up"
    assert m5["high"] >= m5["low"]
    assert len(m5["bars"]) == 8 and len(m5["bars"][0]) == 4
    assert snap["price"]["spread"] == round(104.3 - 104.0, 2)


def test_bars_capped_and_dicts_accepted():
    dict_bars = [{"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05} for _ in range(20)]
    snap = build_price_snapshot("EURUSD", {"M1": dict_bars}, max_bars=6)
    assert len(snap["timeframes"]["M1"]["bars"]) == 6


def test_position_surfaced_when_present():
    pos = SimpleNamespace(direction="LONG", profit_r=0.8, hold_seconds=120.0, size=0.01)
    snap = build_price_snapshot("XAUUSD", {"M5": _uptrend()}, position=pos)
    assert snap["position"]["direction"] == "LONG"
    assert snap["position"]["profit_r"] == 0.8


def test_empty_candles_yields_no_evidence():
    snap = build_price_snapshot("XAUUSD", {})
    assert snap["timeframes"] == {}
    assert snapshot_to_evidence("XAUUSD", snap) == []


def test_snapshot_to_evidence_carries_chart_and_lean():
    snap = build_price_snapshot(
        "XAUUSD", {"M5": _uptrend(), "M15": _uptrend(start=90.0), "H1": _uptrend(start=80.0)},
        tick=SimpleNamespace(bid=104.0, ask=104.2, mid=104.1),
    )
    ev = snapshot_to_evidence("XAUUSD", snap)
    assert len(ev) == 1
    e = ev[0]
    assert e.source_module == "market.price_action"
    assert e.domain.value == "multi_timeframe"
    assert e.polarity > 0.0            # all-up ⇒ modest bullish lean
    assert e.measurements.get("timeframes")   # the actual chart rides along
    assert "chart XAUUSD" in e.observation


def test_downtrend_gives_negative_lean():
    snap = build_price_snapshot("EURUSD", {"M5": _downtrend(), "M15": _downtrend()})
    ev = snapshot_to_evidence("EURUSD", snap)
    assert ev and ev[0].polarity < 0.0


def test_fault_safe_on_garbage():
    snap = build_price_snapshot("X", {"M5": ["garbage", None, 5]})
    # unparseable rows are skipped; no timeframe survives
    assert snap["timeframes"].get("M5") is None
