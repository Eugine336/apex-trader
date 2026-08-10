"""Tests for cognition.market_snapshot — the Brain's reconstructed live chart."""

from types import SimpleNamespace

from cognition.market_snapshot import (
    build_microstructure,
    build_price_snapshot,
    pullback_read,
    session_context,
    snapshot_to_evidence,
    summarize_depth,
)


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


def test_snapshot_to_evidence_carries_chart_non_directional():
    snap = build_price_snapshot(
        "XAUUSD", {"M5": _uptrend(), "M15": _uptrend(start=90.0), "H1": _uptrend(start=80.0)},
        tick=SimpleNamespace(bid=104.0, ask=104.2, mid=104.1),
    )
    ev = snapshot_to_evidence("XAUUSD", snap)
    assert len(ev) == 1
    e = ev[0]
    assert e.source_module == "market.price_action"
    assert e.domain.value == "multi_timeframe"
    assert e.polarity == 0.0           # Part XXV — the chart is reality, not a lean
    assert e.confidence > 0.4          # aligned timeframes ⇒ a clearer picture
    assert e.measurements.get("timeframes")   # the actual chart rides along
    assert "chart XAUUSD" in e.observation


def test_downtrend_is_also_non_directional():
    snap = build_price_snapshot("EURUSD", {"M5": _downtrend(), "M15": _downtrend()})
    ev = snapshot_to_evidence("EURUSD", snap)
    # Part XXV — a fully-aligned downtrend carries no directional lean either;
    # only the raw per-timeframe trend (reality) rides in measurements.
    assert ev and ev[0].polarity == 0.0
    assert ev[0].measurements["timeframes"]["M5"]["trend"] == "down"


def test_fault_safe_on_garbage():
    snap = build_price_snapshot("X", {"M5": ["garbage", None, 5]})
    # unparseable rows are skipped; no timeframe survives
    assert snap["timeframes"].get("M5") is None


def _ticks(mids, *, start_epoch=1_000_000.0, step=1.0, spread=0.2):
    return [
        SimpleNamespace(
            bid=m - spread / 2.0, ask=m + spread / 2.0, mid=m,
            spread=spread, epoch=start_epoch + i * step,
        )
        for i, m in enumerate(mids)
    ]


def test_microstructure_rising_tape():
    micro = build_microstructure(_ticks([100.0, 100.2, 100.5, 100.9]))
    assert micro["ticks"] == 4
    assert micro["up_ticks"] == 3 and micro["down_ticks"] == 0
    assert micro["momentum"] == 1.0            # all up
    assert micro["drift"] > 0                   # net positive drift
    assert micro["velocity_tps"] is not None    # timestamps present
    assert micro["spread_widening"] is False


def test_microstructure_spread_widening_flag():
    ticks = _ticks([100.0, 100.0], spread=0.2)
    ticks[-1] = SimpleNamespace(bid=99.0, ask=101.0, mid=100.0, spread=2.0, epoch=1_000_001.0)
    micro = build_microstructure(ticks)
    assert micro["spread_widening"] is True


def test_microstructure_needs_two_ticks():
    assert build_microstructure([]) == {}
    assert build_microstructure(_ticks([100.0])) == {}


def test_summarize_depth_imbalance_and_top():
    levels = [
        {"price": 100.0, "volume": 5.0, "side": "bid"},
        {"price": 99.9, "volume": 3.0, "side": "bid"},
        {"price": 100.2, "volume": 2.0, "side": "ask"},
    ]
    book = summarize_depth(levels)
    assert book["top_bid"] == 100.0 and book["top_ask"] == 100.2
    assert book["bid_vol"] == 8.0 and book["ask_vol"] == 2.0
    assert book["imbalance"] > 0.0             # bid-heavy ⇒ buy pressure
    assert summarize_depth([]) is None
    assert summarize_depth(None) is None


def test_session_context_windows():
    # 13:00 UTC ⇒ London/NY overlap, high liquidity
    overlap = session_context(13 * 3600)
    assert overlap["session"] == "london_ny_overlap"
    assert overlap["high_liquidity"] is True
    # 03:00 UTC ⇒ Asian, not high liquidity
    asian = session_context(3 * 3600)
    assert asian["session"] == "asian" and asian["high_liquidity"] is False
    assert session_context(0) == {}


def test_pullback_read_dip_in_uptrend():
    tfs = {
        "H4": {"trend": "up", "change_pct": 1.0},
        "M1": {"trend": "down", "change_pct": -0.1},
    }
    read = pullback_read(tfs)
    assert read["read"] == "pullback_in_uptrend"
    assert read["context_trend"] == "up"          # raw structural reality
    assert "with_trend_dir" not in read           # Part XXV — no trade direction
    assert read["context_tf"] == "H4" and read["micro_tf"] == "M1"


def test_pullback_read_bounce_in_downtrend():
    tfs = {
        "H1": {"trend": "down", "change_pct": -0.8},
        "M5": {"trend": "up", "change_pct": 0.2},
    }
    read = pullback_read(tfs)
    assert read["read"] == "bounce_in_downtrend"
    assert read["context_trend"] == "down"
    assert "with_trend_dir" not in read


def test_pullback_read_needs_two_known_tfs():
    assert pullback_read({"M5": {"trend": "up"}}) == {}
    assert pullback_read({}) == {}


def test_build_snapshot_wires_microstructure_depth_session_pullback():
    snap = build_price_snapshot(
        "XAUUSD",
        {"H4": _uptrend(start=80.0), "M1": _downtrend(start=104.0, step=0.05)},
        tick=SimpleNamespace(bid=104.0, ask=104.3, mid=104.15),
        ticks=_ticks([104.0, 104.1, 104.05, 104.2]),
        depth=[
            {"price": 104.0, "volume": 5.0, "side": "bid"},
            {"price": 104.3, "volume": 2.0, "side": "ask"},
        ],
        now_epoch=13 * 3600,
        max_bars=6,
    )
    assert snap["microstructure"] and snap["microstructure"]["ticks"] == 4
    assert snap["depth"] and snap["depth"]["imbalance"] > 0.0
    assert snap["session"]["session"] == "london_ny_overlap"
    assert snap["pullback"]["read"] == "pullback_in_uptrend"
    ev = snapshot_to_evidence("XAUUSD", snap)
    assert ev and "pullback_in_uptrend" in ev[0].observation
    assert ev[0].measurements.get("microstructure")


def test_optional_blocks_absent_when_inputs_missing():
    snap = build_price_snapshot("EURUSD", {"M5": _uptrend()})
    assert snap["microstructure"] is None
    assert snap["depth"] is None
    assert snap["session"] is None


# ── Violation #3 — richer multi-scale microstructure ────────────────────────


def test_microstructure_keeps_backward_compatible_keys():
    # Every legacy key must still be present alongside the new ones so existing
    # consumers keep working.
    micro = build_microstructure(_ticks([100.0, 100.2, 100.5, 100.9]))
    for k in (
        "ticks", "window_seconds", "velocity_tps", "drift", "drift_pct",
        "up_ticks", "down_ticks", "momentum", "range",
        "spread_last", "spread_mean", "spread_max", "spread_widening",
    ):
        assert k in micro


def test_microstructure_multiscale_windows_over_deep_tape():
    # A ~40s rising tape (1 tick/sec) — the multi-scale reads should all appear.
    mids = [100.0 + 0.1 * i for i in range(40)]
    micro = build_microstructure(_ticks(mids))
    mw = micro.get("momentum_windows")
    assert mw and set(mw) == {"2s", "10s", "30s"}
    assert micro.get("velocity_windows") and set(micro["velocity_windows"]) == {"2s", "10s", "30s"}
    assert micro.get("drift_windows")
    # A monotonic rise ⇒ every window's momentum is positive.
    assert all(v > 0 for v in mw.values())
    # The 30s window drift exceeds the 2s window drift (more ground covered).
    assert micro["drift_windows"]["30s"] > micro["drift_windows"]["2s"]


def test_microstructure_exhaustion_on_decelerating_move():
    # Big up-steps early, tiny up-steps late — a directional move losing fuel.
    mids = [100.0, 100.5, 101.0, 101.5, 102.0, 102.05, 102.08, 102.10, 102.11, 102.115]
    micro = build_microstructure(_ticks(mids))
    assert micro["velocity_trend"] == "decelerating"
    assert micro["velocity_accel"] < 0
    assert micro["exhaustion"] is True
    assert 0.0 < micro["exhaustion_score"] <= 1.0
    assert micro["absorption"] is False


def test_microstructure_acceleration_flag():
    # Tiny early, big late — the move is speeding up.
    mids = [100.0, 100.01, 100.02, 100.03, 100.05, 100.2, 100.5, 101.0]
    micro = build_microstructure(_ticks(mids))
    assert micro["velocity_trend"] == "accelerating"
    assert micro["velocity_accel"] > 0


def test_microstructure_absorption_when_pinned_but_active():
    # Many ticks, price oscillating inside a band ~ the spread — orders absorbed.
    mids = [100.0 + (0.02 if i % 2 else -0.02) for i in range(30)]
    micro = build_microstructure(_ticks(mids, spread=0.05))
    assert micro["ticks"] == 30
    assert micro["absorption"] is True
    assert 0.0 < micro["absorption_score"] <= 1.0
    assert micro["exhaustion"] is False


def test_microstructure_spread_trend_widening():
    mids = [100.0 + 0.01 * i for i in range(20)]
    ticks = _ticks(mids, spread=0.1)
    for i in range(10, 20):  # widen the spread across the late half
        ticks[i] = SimpleNamespace(
            bid=mids[i] - 0.3, ask=mids[i] + 0.3, mid=mids[i], spread=0.6,
            epoch=ticks[i].epoch,
        )
    micro = build_microstructure(ticks)
    assert micro["spread_trend"] == "widening"
    assert micro["spread_trend_ratio"] > 1.2


def test_microstructure_tick_rate_burst():
    base = 1_000_000.0
    def _mk(mid, ep):
        return SimpleNamespace(bid=mid - 0.1, ask=mid + 0.1, mid=mid, spread=0.2, epoch=ep)
    # First ~10s at 1 tick/sec, then a dense burst of 20 ticks in the final ~1s.
    early = [_mk(100.0 + 0.001 * i, base + float(i)) for i in range(11)]
    late = [_mk(100.01 + 0.0005 * i, base + 10.0 + 0.05 * (i + 1)) for i in range(20)]
    micro = build_microstructure(early + late)
    assert micro.get("tick_rate_change") == "burst"
    assert micro["tick_rate_ratio"] > 1.5


def test_microstructure_no_timestamps_degrades_gracefully():
    ticks = [
        SimpleNamespace(bid=m - 0.1, ask=m + 0.1, mid=m, spread=0.2, epoch=0.0)
        for m in (100.0, 100.5, 101.0)
    ]
    micro = build_microstructure(ticks)
    # Base keys survive; time-only multi-scale windows are omitted; the
    # index-based velocity trend is still computed.
    assert micro["ticks"] == 3
    assert "momentum_windows" not in micro
    assert micro.get("velocity_trend") in ("accelerating", "decelerating", "steady")


def test_microstructure_two_ticks_still_fault_safe():
    # The enrichment must never drop the base read even on a minimal tape.
    micro = build_microstructure(_ticks([100.0, 100.2]))
    assert micro["ticks"] == 2 and "momentum" in micro


