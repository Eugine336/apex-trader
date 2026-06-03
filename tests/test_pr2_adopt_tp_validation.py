"""Tests for PR-2: orphan-adoption TP1 validation in RecoveryReconciliationMixin.

Exercises _validated_adopted_tp1() — the pure helper that decides whether
to keep the broker TP, reconstruct from entry ± 1.5×risk, or disable TP
management (return 0.0).

CI is the runtime authority — pytest cannot run in the sandbox.
"""

from platforms.trading_loop.recovery_mixin import RecoveryReconciliationMixin


class TestValidatedAdoptedTp1:
    """Unit tests for the static helper — no mixin instantiation needed."""

    # ── Case 1: long, broker_tp=0, valid SL below entry → reconstruct ──

    def test_long_zero_tp_valid_sl_reconstructs_above_entry(self):
        entry = 1.10000
        sl = 1.09500  # 50-pip risk
        tp1 = RecoveryReconciliationMixin._validated_adopted_tp1(
            direction="BUY", entry_price=entry, sl=sl, broker_tp=0.0,
        )
        expected = round(entry + 1.5 * abs(entry - sl), 8)  # 1.10750
        assert tp1 == expected
        assert tp1 > entry

    # ── Case 2: short, broker_tp=0, valid SL above entry → reconstruct ──

    def test_short_zero_tp_valid_sl_reconstructs_below_entry(self):
        entry = 1.10000
        sl = 1.10500  # 50-pip risk
        tp1 = RecoveryReconciliationMixin._validated_adopted_tp1(
            direction="SELL", entry_price=entry, sl=sl, broker_tp=0.0,
        )
        expected = round(entry - 1.5 * abs(entry - sl), 8)  # 1.09250
        assert tp1 == expected
        assert tp1 < entry

    # ── Case 3: long, broker_tp BELOW entry (wrong side) → reconstruct ──

    def test_long_tp_below_entry_reconstructs(self):
        entry = 1.10000
        sl = 1.09700  # 30-pip risk
        broker_tp = 1.09500  # below entry → invalid for a long
        tp1 = RecoveryReconciliationMixin._validated_adopted_tp1(
            direction="BUY", entry_price=entry, sl=sl, broker_tp=broker_tp,
        )
        expected = round(entry + 1.5 * abs(entry - sl), 8)  # 1.10450
        assert tp1 == expected
        assert tp1 > entry
        assert tp1 != broker_tp

    # ── Case 4: long, valid broker_tp ABOVE entry → returned unchanged ──

    def test_long_valid_tp_above_entry_kept(self):
        entry = 1.10000
        sl = 1.09700
        broker_tp = 1.10500  # valid: above entry for a long
        tp1 = RecoveryReconciliationMixin._validated_adopted_tp1(
            direction="BUY", entry_price=entry, sl=sl, broker_tp=broker_tp,
        )
        assert tp1 == broker_tp

    # ── Case 5: long, no usable SL → TP disabled (0.0) ──

    def test_long_zero_tp_no_sl_returns_zero(self):
        entry = 1.10000
        tp1 = RecoveryReconciliationMixin._validated_adopted_tp1(
            direction="BUY", entry_price=entry, sl=0.0, broker_tp=0.0,
        )
        assert tp1 == 0.0

    def test_long_zero_tp_sl_equals_entry_returns_zero(self):
        entry = 1.10000
        tp1 = RecoveryReconciliationMixin._validated_adopted_tp1(
            direction="BUY", entry_price=entry, sl=entry, broker_tp=0.0,
        )
        assert tp1 == 0.0

    # ── Short variants mirror long ──

    def test_short_valid_tp_below_entry_kept(self):
        entry = 1.10000
        sl = 1.10300
        broker_tp = 1.09500  # valid: below entry for a short
        tp1 = RecoveryReconciliationMixin._validated_adopted_tp1(
            direction="SELL", entry_price=entry, sl=sl, broker_tp=broker_tp,
        )
        assert tp1 == broker_tp

    def test_short_tp_above_entry_reconstructs(self):
        entry = 1.10000
        sl = 1.10300
        broker_tp = 1.10500  # above entry → invalid for a short
        tp1 = RecoveryReconciliationMixin._validated_adopted_tp1(
            direction="SELL", entry_price=entry, sl=sl, broker_tp=broker_tp,
        )
        expected = round(entry - 1.5 * abs(entry - sl), 8)
        assert tp1 == expected
        assert tp1 < entry

    def test_short_zero_tp_no_sl_returns_zero(self):
        tp1 = RecoveryReconciliationMixin._validated_adopted_tp1(
            direction="SELL", entry_price=1.10000, sl=0.0, broker_tp=0.0,
        )
        assert tp1 == 0.0

    # ── Edge cases ──

    def test_negative_broker_tp_treated_as_invalid(self):
        entry = 1.10000
        sl = 1.09500
        tp1 = RecoveryReconciliationMixin._validated_adopted_tp1(
            direction="BUY", entry_price=entry, sl=sl, broker_tp=-1.0,
        )
        assert tp1 > entry

    def test_direction_case_insensitive_long(self):
        entry = 100.0
        sl = 99.0
        tp1 = RecoveryReconciliationMixin._validated_adopted_tp1(
            direction="buy", entry_price=entry, sl=sl, broker_tp=0.0,
        )
        assert tp1 > entry

    def test_direction_case_insensitive_short(self):
        entry = 100.0
        sl = 101.0
        tp1 = RecoveryReconciliationMixin._validated_adopted_tp1(
            direction="sell", entry_price=entry, sl=sl, broker_tp=0.0,
        )
        assert tp1 < entry

    def test_large_instrument_prices(self):
        entry = 2700.00
        sl = 2695.00
        tp1 = RecoveryReconciliationMixin._validated_adopted_tp1(
            direction="BUY", entry_price=entry, sl=sl, broker_tp=0.0,
        )
        expected = round(entry + 1.5 * 5.0, 8)  # 2707.50
        assert tp1 == expected
