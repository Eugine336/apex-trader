"""Tests for the tick-rule order-flow delta analyzer (entry/tick_delta_analyzer.py).

Covers the aggressor-delta computation (all-buy, all-sell, balanced), the
direction-aware flip confirmation, graceful degradation below the minimum tick
count, threshold edge cases and the ``last``-price preference over the mid.
"""

from types import SimpleNamespace

from entry.tick_delta_analyzer import DeltaResult, TickDeltaAnalyzer


def _ticks(prices, spread=0.01):
    """Build SimpleNamespace ticks (bid/ask) from a list of mid-ish prices."""
    return [SimpleNamespace(bid=p, ask=p + spread) for p in prices]


class TestDeltaRatio:
    def test_all_buy_ticks_ratio_plus_one_confirms_long(self):
        ticks = _ticks([2000.0 + i for i in range(12)])  # monotonically rising
        a = TickDeltaAnalyzer()
        raw = a.analyze(ticks)
        assert raw.buy_count == 11
        assert raw.sell_count == 0
        assert raw.delta_ratio == 1.0
        # +1 delta confirms a LONG flip, rejects a SHORT flip.
        assert a.confirm(ticks, "LONG", 0.2).confirmed is True
        assert a.confirm(ticks, "SHORT", 0.2).confirmed is False

    def test_all_sell_ticks_ratio_minus_one_confirms_short(self):
        ticks = _ticks([2000.0 - i for i in range(12)])  # monotonically falling
        a = TickDeltaAnalyzer()
        raw = a.analyze(ticks)
        assert raw.sell_count == 11
        assert raw.buy_count == 0
        assert raw.delta_ratio == -1.0
        assert a.confirm(ticks, "SHORT", 0.2).confirmed is True
        assert a.confirm(ticks, "LONG", 0.2).confirmed is False

    def test_balanced_ticks_ratio_near_zero_rejects_both(self):
        # 13 alternating ticks → 12 pairs → 6 up, 6 down → delta 0.0 exactly.
        alt = [2000.0 + (i % 2) for i in range(13)]
        a = TickDeltaAnalyzer()
        raw = a.analyze(_ticks(alt))
        assert raw.buy_count == 6
        assert raw.sell_count == 6
        assert raw.delta_ratio == 0.0
        assert a.confirm(_ticks(alt), "LONG", 0.2).confirmed is False
        assert a.confirm(_ticks(alt), "SHORT", 0.2).confirmed is False

    def test_neutral_ticks_excluded_from_ratio(self):
        # 6 up, 4 down, plus 2 flat (neutral) pairs — ratio uses buy+sell only.
        prices = [100, 101, 102, 103, 104, 105, 106, 105, 104, 103, 102, 102, 102]
        raw = TickDeltaAnalyzer().analyze(_ticks(prices))
        assert raw.buy_count == 6
        assert raw.sell_count == 4
        assert raw.neutral_count == 2
        assert abs(raw.delta_ratio - 0.2) < 1e-9


class TestInsufficientTicks:
    def test_too_few_ticks_skips_gracefully(self):
        few = _ticks([2000.0 + i for i in range(5)])  # 5 < MIN_TICKS (10)
        raw = TickDeltaAnalyzer().analyze(few)
        assert raw.reason == "insufficient_ticks"
        assert raw.confirmed is False
        # confirm passes the insufficient result straight through (a skip).
        res = TickDeltaAnalyzer().confirm(few, "LONG", 0.2)
        assert res.reason == "insufficient_ticks"
        assert res.confirmed is False

    def test_empty_and_none_are_insufficient(self):
        a = TickDeltaAnalyzer()
        assert a.analyze([]).reason == "insufficient_ticks"
        assert a.analyze(None).reason == "insufficient_ticks"

    def test_malformed_ticks_dropped(self):
        # Non-positive / missing prices are skipped, dropping the sample below
        # the minimum → an insufficient (skip) result rather than a crash.
        junk = [SimpleNamespace(bid=0.0, ask=0.0) for _ in range(12)]
        assert TickDeltaAnalyzer().analyze(junk).reason == "insufficient_ticks"


class TestThresholdEdges:
    def test_ratio_exactly_at_threshold_confirms(self):
        # 6 up then 4 down over 10 pairs → delta_ratio = 0.2.
        prices = [100, 101, 102, 103, 104, 105, 106, 105, 104, 103, 102]
        ticks = _ticks(prices)
        a = TickDeltaAnalyzer()
        assert a.confirm(ticks, "LONG", 0.2).confirmed is True  # >= threshold
        assert a.confirm(ticks, "LONG", 0.25).confirmed is False  # below higher bar
        assert a.confirm(ticks, "SHORT", 0.2).confirmed is False

    def test_negative_threshold_is_treated_as_magnitude(self):
        ticks = _ticks([2000.0 + i for i in range(12)])
        # A negative threshold is abs()-normalised, so it behaves like +0.2.
        assert TickDeltaAnalyzer().confirm(ticks, "LONG", -0.2).confirmed is True

    def test_unknown_direction_skips(self):
        ticks = _ticks([2000.0 + i for i in range(12)])
        res = TickDeltaAnalyzer().confirm(ticks, "SIDEWAYS", 0.2)
        assert res.reason == "unknown_direction"
        assert res.confirmed is False


class TestReferencePrice:
    def test_last_price_preferred_over_mid(self):
        # bid/ask flat but the ``last`` trade price rises → all-buy by tick rule.
        ticks = [SimpleNamespace(bid=100.0, ask=100.02, last=100.0 + i * 0.01) for i in range(12)]
        raw = TickDeltaAnalyzer().analyze(ticks)
        assert raw.buy_count == 11
        assert raw.sell_count == 0
        assert raw.delta_ratio == 1.0

    def test_dict_ticks_supported(self):
        ticks = [{"bid": 2000.0 + i, "ask": 2000.0 + i + 0.01} for i in range(12)]
        raw = TickDeltaAnalyzer().analyze(ticks)
        assert isinstance(raw, DeltaResult)
        assert raw.delta_ratio == 1.0

    def test_custom_min_ticks(self):
        a = TickDeltaAnalyzer(min_ticks=4)
        raw = a.analyze(_ticks([2000.0 + i for i in range(4)]))
        assert raw.reason == "ok"
        assert raw.delta_ratio == 1.0
