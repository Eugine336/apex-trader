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
from platforms.base_connector import TickData


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


def _make_connector():
    """Build a DerivConnector without running __init__ (no event-loop thread)."""
    conn = object.__new__(DerivConnector)
    conn._discovered_multipliers = {}
    conn._positions = {}
    conn._require_connection = lambda: None
    conn.symbol_map = lambda s: s
    conn.get_price = lambda s: TickData(bid=7956.9, ask=7956.9, spread=0.0, time=0.0)
    conn._accepted_multipliers = lambda m: (_ACCEPTED, 1000)
    return conn


class TestPlaceOrderRetryPath:
    """Regression for the live ``name 'sl_pct' is not defined`` crash.

    When Deriv rejects the first attempt with 'Input validation failed:
    parameters', place_order retries and recomputes the limit_order dollar
    values via _limit_order_dollars(sl_pct, tp_pct, ...). Those fraction-of-price
    variables must be in scope on the retry path — previously only
    sl_pct_initial existed and the retry raised NameError before the order
    could be re-sent."""

    def test_retry_after_validation_error_does_not_raise(self):
        conn = _make_connector()
        calls = {"n": 0}

        def fake_send(payload):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"error": {"message": "Input validation failed: parameters"}}
            return {"buy": {"contract_id": "12345"}}

        conn._sync_send = fake_send

        result = conn.place_order(
            symbol="STPIDX", direction="SHORT", lots=0.01,
            sl=7980.67, tp=7920.99, stake_usd=27.99, multiplier=200,
        )

        assert calls["n"] == 2  # first failed → retried
        assert result.success is True
        assert result.order_id == "12345"

    def test_retry_recomputes_limit_order_within_stake(self):
        conn = _make_connector()
        seen_payloads = []

        def fake_send(payload):
            seen_payloads.append(payload)
            if len(seen_payloads) == 1:
                return {"error": {"message": "Input validation failed: parameters"}}
            return {"buy": {"contract_id": "999"}}

        conn._sync_send = fake_send

        result = conn.place_order(
            symbol="STPIDX", direction="SHORT", lots=0.01,
            sl=7980.67, tp=7920.99, stake_usd=27.99, multiplier=200,
        )

        assert result.success is True
        # The retry drops limit_order (the validation error path disables it),
        # so the second payload must not carry SL/TP bounds.
        assert "limit_order" not in seen_payloads[1]["parameters"]

