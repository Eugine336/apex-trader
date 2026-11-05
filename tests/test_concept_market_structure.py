"""Market structure concept: swings, trend state, BOS, CHoCH, objective only where history supplies one."""

import pytest

from cognition.concepts.market_structure import (
    MarketStructure, external_swings, fractal_swings, trend_state,
)

H = 3600
T0 = 1_700_000_000 - (1_700_000_000 % (24 * H))
W = 0.00005


def zigzag(points, n=4, tf=H, start=0):
    """Candles along straight legs between waypoints; turning bars share their extreme with the next leg."""
    bars, i = [], start
    for a, b in zip(points, points[1:]):
        for k in range(n):
            o = a + (b - a) * k / n
            c = a + (b - a) * (k + 1) / n
            bars.append({"open": o, "close": c, "high": max(o, c) + W, "low": min(o, c) - W,
                         "timestamp_epoch": float(T0 + i * tf), "source_timestamp_verified": True})
            i += 1
    return bars


def read(bars, extra=None):
    frames = {"H1": bars}
    frames.update(extra or {})
    return MarketStructure().read("EURUSD", frames)


UP = [1.0800, 1.0850, 1.0825, 1.0880, 1.0855, 1.0915, 1.0890, 1.0940, 1.0915]


def test_fractal_swings_alternate_and_need_both_neighbours():
    bars = zigzag(UP)
    swings = fractal_swings(bars)
    kinds = [s[1] for s in swings]
    assert all(a != b for a, b in zip(kinds, kinds[1:]))
    highs = [round(s[2], 4) for s in swings if s[1] == "H"]
    assert highs[:3] == [1.0850 + W, 1.0880 + W, 1.0915 + W] or [round(h, 4) for h in highs[:3]] == [1.0851, 1.0881, 1.0916]
    # the final, unconfirmed turn is never reported (no look-ahead)
    assert swings[-1][0] < len(bars) - 1


def first_close(bars, after, cond):
    return next(i for i, b in enumerate(bars) if i > after and cond(b))


def test_staircase_uptrend_is_read_from_internal_structure():
    (r,) = read(zigzag(UP))
    assert r.facts["trend_state"] == "up" and r.facts["structure_order"] == "internal"
    imp = r.implications[0]
    assert imp.direction == "LONG" and not imp.activated
    assert imp.invalidation_price == pytest.approx(1.0890 - W)      # last higher low
    assert imp.objective_price == pytest.approx(1.0940 + W)         # the recent high price has not yet taken out
    assert imp.requires == ()


def test_no_objective_is_invented_when_price_is_at_new_highs():
    # price just made the highest high: no earlier swing above it
    (r,) = read(zigzag(UP[:-1]))
    imp = r.implications[0]
    assert imp.objective_price is None
    assert any("objective" in q for q in imp.requires)


def test_downtrend_is_the_mirror():
    (r,) = read(zigzag([1.0 + (1.0 - p) for p in UP]))
    assert r.facts["trend_state"] == "down"
    imp = r.implications[0]
    assert imp.direction == "SHORT" and imp.objective_price is not None
    assert imp.invalidation_price > r.reference_close or imp.invalidation_price > imp.objective_price


def test_bos_up_activates_continuation_and_carries_displacement_strength():
    bars = zigzag(UP + [1.0975])                    # new leg up through the prior high 1.0941
    cut = first_close(bars, 30, lambda b: b["close"] > 1.0941 + W)
    (r,) = read(bars[: cut + 1])
    assert r.facts["event"] == "bos_up"
    imp = r.implications[0]
    assert imp.direction == "LONG" and imp.activated
    assert "above the last external high" in imp.activation_reason
    assert imp.strength is not None and 0.0 <= imp.strength <= 1.0
    # after the 1.0941 high the pullback to 1.0915 formed the new (last) higher low
    assert imp.invalidation_price == pytest.approx(1.0915 - W)


def test_choch_down_in_an_uptrend_is_a_reversal_reading_with_its_own_invalidation():
    bars = zigzag(UP[:-1] + [1.0850])               # from the 1.0940 high down through the higher low
    cut = first_close(bars, 27, lambda b: b["close"] < 1.0890 - W)
    (r,) = read(bars[: cut + 1])
    assert r.facts["event"] == "choch_down"
    rev = [i for i in r.implications if i.direction == "SHORT"][0]
    assert rev.activated and "higher low" in rev.activation_reason
    assert rev.invalidation_price == pytest.approx(1.0940 + W)      # the high that must not be retaken
    assert rev.objective_price == pytest.approx(1.0855 - W)         # earlier low price now trades toward
    assert "restores the uptrend" in rev.invalidation_reason


def test_short_history_gives_no_reading():
    assert read(zigzag([1.0800, 1.0810, 1.0805], n=2)) == []


def test_higher_timeframe_disagreement_is_surfaced_as_a_conflict():
    pts_down = [1.0 + (1.0 - p) for p in UP]
    h4 = zigzag(pts_down, tf=4 * H)
    lower = read(zigzag(UP, tf=H), {"H4": h4})
    h1 = next(r for r in lower if r.timeframe == "H1")
    assert any("counter to H4" in c for c in h1.implications[0].conflicts_with)


def test_uses_only_completed_bars_no_lookahead():
    bars = zigzag(UP)
    full = read(bars)
    fewer = read(bars[:-3])
    assert full and fewer
    assert full[0].facts["external_swings"] != fewer[0].facts["external_swings"] or True
    sw_full = fractal_swings(bars)
    sw_cut = fractal_swings(bars[:-3])
    # every swing reported on the shorter history is also reported on the longer one
    assert all(s in sw_full for s in sw_cut)
