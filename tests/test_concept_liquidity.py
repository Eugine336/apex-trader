"""Liquidity concept: untouched pools, sweeps with rejection, objectives only from real pools."""

import pytest

from cognition.concepts.liquidity import LiquidityPools

H = 3600
T0 = 1_700_000_000 - (1_700_000_000 % (24 * H))
W = 0.00005


def zigzag(points, n=4, tf=H, start=0):
    bars, i = [], start
    for a, b in zip(points, points[1:]):
        for k in range(n):
            o = a + (b - a) * k / n
            c = a + (b - a) * (k + 1) / n
            bars.append({"open": o, "close": c, "high": max(o, c) + W, "low": min(o, c) - W,
                         "timestamp_epoch": float(T0 + i * tf), "source_timestamp_verified": True})
            i += 1
    return bars


def bar(i, o, h, l, c, tf=H):
    return {"open": o, "high": h, "low": l, "close": c, "timestamp_epoch": float(T0 + i * tf),
            "source_timestamp_verified": True}


RANGE = [1.0800, 1.0850, 1.0820, 1.0852, 1.0818, 1.0849, 1.0825]     # equal highs ~1.085, equal lows ~1.082


def read(bars, extra=None):
    frames = {"H1": bars}
    frames.update(extra or {})
    return LiquidityPools().read("EURUSD", frames)


def test_untouched_equal_highs_and_lows_are_reported_as_draws_on_liquidity():
    (r,) = read(zigzag(RANGE))
    prices_above = [round(p["price"], 4) for p in r.facts["pools_above"]]
    assert 1.0852 in prices_above or 1.0853 in prices_above
    assert r.implications == ()                       # pools alone imply no side
    assert r.facts["swept"] is None


def test_buy_side_sweep_with_rejection_implies_short_with_objective_at_sell_side_pool():
    bars = zigzag(RANGE)
    i = len(bars)
    bars.append(bar(i, 1.0840, 1.0866, 1.0838, 1.0842))       # wicks above the 1.0852 highs, closes back below
    (r,) = read(bars)
    imp = r.implications[0]
    assert imp.direction == "SHORT" and imp.activated
    assert imp.invalidation_price == pytest.approx(1.0866)
    assert imp.objective_price is not None and imp.objective_price < 1.0842     # nearest untouched sell-side pool
    assert r.facts["swept"]["kind"] == "H"
    assert any("lower-timeframe" in q for q in imp.requires)
    assert imp.strength is None or 0.0 <= imp.strength <= 1.0


def test_sell_side_sweep_implies_long():
    bars = zigzag(RANGE)
    i = len(bars)
    bars.append(bar(i, 1.0830, 1.0834, 1.0805, 1.0828))       # wicks below the 1.0818 lows, closes back above
    (r,) = read(bars)
    imp = r.implications[0]
    assert imp.direction == "LONG" and imp.invalidation_price == pytest.approx(1.0805)
    assert imp.objective_price is not None and imp.objective_price > 1.0828


def test_close_beyond_the_pool_is_acceptance_not_a_sweep():
    bars = zigzag(RANGE)
    i = len(bars)
    bars.append(bar(i, 1.0845, 1.0870, 1.0844, 1.0866))       # closes above the highs
    rs = read(bars)
    assert all(x.facts["swept"] is None for x in rs)


def test_a_pool_that_was_already_traded_through_is_no_longer_a_pool():
    bars = zigzag(RANGE)
    i = len(bars)
    bars.append(bar(i, 1.0845, 1.0875, 1.0844, 1.0870))       # takes the highs out (acceptance)
    bars.append(bar(i + 1, 1.0866, 1.0880, 1.0850, 1.0854))   # later bar wicks above 1.0852 again
    rs = read(bars)
    assert all(x.facts["swept"] is None or x.facts["swept"]["pool_price"] > 1.0875 for x in rs)


def test_previous_day_high_is_a_pool_until_traded_through():
    day = 86400
    d1 = [{"open": 1.08, "high": 1.0900, "low": 1.0780, "close": 1.0850,
           "timestamp_epoch": float(T0 - 2 * day), "source_timestamp_verified": True},
          {"open": 1.085, "high": 1.0890, "low": 1.0790, "close": 1.0840,
           "timestamp_epoch": float(T0 - day), "source_timestamp_verified": True}]
    bars = zigzag(RANGE)
    (r,) = read(bars, {"D1": d1}) if len(read(bars, {"D1": d1})) == 1 else [read(bars, {"D1": d1})[0]]
    sources = [p["source"] for p in r.facts["pools_above"]]
    assert "previous day high" in sources
    # a sweep of that level
    i = len(bars)
    bars2 = bars + [bar(i, 1.0870, 1.0900 + 0.0006, 1.0868, 1.0880)]
    r2 = [x for x in read(bars2, {"D1": d1}) if x.timeframe == "H1"][0]
    assert r2.facts["swept"] is not None and r2.facts["swept"]["source"] in ("previous day high", "swing")


def test_short_history_gives_no_reading():
    assert read(zigzag([1.0800, 1.0810, 1.0805], n=2)) == []


def test_no_lookahead_the_latest_bar_cannot_create_the_pool_it_sweeps():
    bars = zigzag(RANGE)
    i = len(bars)
    only_latest = bars + [bar(i, 1.0830, 1.0880, 1.0829, 1.0835)]   # new extreme: nothing before it to sweep
    (r,) = read(only_latest)
    assert r.facts["swept"] is None or r.facts["swept"]["pool_price"] < 1.0880
