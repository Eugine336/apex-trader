"""Tests for the Deriv multiplier-fit + limit_order clamping fix.

A Deriv multiplier contract liquidates at a loss equal to the stake, so the
``limit_order.stop_loss`` dollar value (stake × multiplier × sl_pct) can never
exceed the stake. A too-high multiplier against a wide SL produced a stop_loss
larger than the stake → Deriv rejected the order with
'Input validation failed: parameters'. These tests cover the two pure helpers
that prevent it: snapping the multiplier down so the SL fits, and clamping the
limit_order dollar values as a last-resort safety net.
"""

from platforms.deriv.deriv_connector import DerivConnector, _SL_STAKE_SAFETY


_ACCEPTED = [80, 200, 400, 600, 800, 1000, 2000, 4000]


class TestFitMultiplierForSl:
    def test_wide_sl_snaps_multiplier_down(self):
        # The live-log case: 1000× against a 0.339% SL would lose 3.4× the stake.
        fitted = DerivConnector._fit_multiplier_for_sl(1000, 0.00339, _ACCEPTED)
        assert fitted <= 1000
        # Loss-at-SL must stay within the stake (with the commission buffer).
        assert fitted * 0.00339 <= _SL_STAKE_SAFETY + 1e-9
        assert fitted == 200

    def test_tight_sl_keeps_multiplier(self):
        fitted = DerivConnector._fit_multiplier_for_sl(1000, 0.0001, _ACCEPTED)
        assert fitted == 1000

    def test_never_exceeds_requested_multiplier(self):
        fitted = DerivConnector._fit_multiplier_for_sl(200, 0.0001, _ACCEPTED)
        assert fitted <= 200

    def test_falls_back_to_smallest_when_nothing_fits(self):
        # An extreme SL no accepted multiplier can honour → smallest, minimising
        # the overshoot (the dollar clamp then caps the stop).
        fitted = DerivConnector._fit_multiplier_for_sl(1000, 0.5, _ACCEPTED)
        assert fitted == min(_ACCEPTED)

    def test_zero_sl_pct_is_inert(self):
        assert DerivConnector._fit_multiplier_for_sl(1000, 0.0, _ACCEPTED) == 1000


class TestLimitOrderDollars:
    def test_stop_loss_clamped_within_stake(self):
        # 0.339% SL at 1000× on a $27.99 stake would compute a $94 stop_loss.
        sl, tp = DerivConnector._limit_order_dollars(0.00339, 0.005, 27.99, 1000)
        assert sl <= round(27.99 * _SL_STAKE_SAFETY, 2)
        assert sl <= 27.99
        assert tp > 0

    def test_within_bounds_passes_through(self):
        # After the multiplier is fitted to 200× the stop fits without clamping.
        sl, tp = DerivConnector._limit_order_dollars(0.00339, 0.005, 27.99, 200)
        expected = round(0.00339 * 27.99 * 200, 2)
        assert abs(sl - expected) < 0.01
        assert sl < 27.99

    def test_minimum_positive_floor(self):
        sl, tp = DerivConnector._limit_order_dollars(0.0, 0.0, 10.0, 100)
        assert sl >= 0.01
        assert tp >= 0.01

    def test_values_are_rounded_to_cents(self):
        sl, tp = DerivConnector._limit_order_dollars(0.0012345, 0.0098765, 13.37, 200)
        assert sl == round(sl, 2)
        assert tp == round(tp, 2)
