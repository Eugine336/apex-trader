"""Tests for the opportunistic-trading rewire (Session 1).

Covers the two market-driven behaviours introduced when the system stopped
pre-labelling trades as scalp/swing:

  * ``WorldModel.get_structural_targets`` — take-profit levels come from the
    next FVG / order block / liquidity pool ahead of price, not a hardcoded
    reward multiple.
  * The opportunity ranker clusters by DIRECTION only and scores every idea on
    a single reward:risk proxy.

These are dependency-light where possible; the WorldModel tests construct a
frozen model with duck-typed structural stubs (no pandas/numpy maths).
"""

from datetime import datetime, timezone

from brain.world_model import WorldModel


class _FVG:
    def __init__(self, midpoint):
        self.midpoint = midpoint


class _OB:
    def __init__(self, top, bottom):
        self.top = top
        self.bottom = bottom


class _Zone:
    def __init__(self, price):
        self.price = price


class _LiqMap:
    def __init__(self, buy_side, sell_side):
        self.buy_side_liquidity = buy_side
        self.sell_side_liquidity = sell_side


def _wm(**kw) -> WorldModel:
    return WorldModel(
        symbol="EURUSD",
        version=1,
        timestamp=datetime.now(timezone.utc),
        **kw,
    )


class TestStructuralTargets:
    def test_long_targets_are_levels_above_price(self):
        wm = _wm(fvgs=(("H1", (_FVG(1.1050), _FVG(1.0950))),))
        # current price 1.1000 → only the 1.1050 gap is ahead for a LONG.
        targets = wm.get_structural_targets("LONG", 1.1000)
        assert targets == [1.1050]

    def test_short_targets_are_levels_below_price(self):
        wm = _wm(order_blocks=(("H4", (_OB(top=1.0950, bottom=1.0930),)),))
        # SHORT target uses the OB's near edge (top) when it is below price.
        targets = wm.get_structural_targets("SHORT", 1.1000)
        assert targets == [1.0950]

    def test_targets_sorted_nearest_first(self):
        wm = _wm(
            fvgs=(("H1", (_FVG(1.1080), _FVG(1.1020))),),
            liquidity=(("H1", _LiqMap([_Zone(1.1050)], [])),),
        )
        targets = wm.get_structural_targets("LONG", 1.1000)
        assert targets == [1.1020, 1.1050, 1.1080]

    def test_min_distance_skips_too_close_targets(self):
        wm = _wm(fvgs=(("H1", (_FVG(1.10005), _FVG(1.1050))),))
        # With a 20-pip min distance the 0.5-pip gap is skipped.
        targets = wm.get_structural_targets("LONG", 1.1000, min_distance=0.0020)
        assert targets == [1.1050]

    def test_no_structure_returns_empty(self):
        assert _wm().get_structural_targets("LONG", 1.1000) == []

    def test_invalid_direction_returns_empty(self):
        wm = _wm(fvgs=(("H1", (_FVG(1.1050),)),))
        assert wm.get_structural_targets("FLAT", 1.1000) == []
