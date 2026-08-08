"""
Tests for the MT5 R:R-preservation fix.

When the broker's minimum stop distance widens the SL, the TP is extended
outward to restore the originally-approved reward:risk instead of rejecting the
order. ``_restore_tp_for_rr`` only ever increases reward (never adds risk).
"""

from platforms.mt5.mt5_connector import MT5Connector


def test_preserve_rr_flag_defaults_on():
    c = MT5Connector()
    assert c._preserve_rr_after_adjust is True


def test_long_tp_extended_to_restore_rr():
    # EURAUD-like: entry 1.64743, original SL 1.64698 (risk 0.00045), R:R 3.0.
    # Broker widens SL to 1.64693 (risk 0.00050); TP must extend to keep 3.0.
    price, sl, tp, rr = 1.64743, 1.64693, 1.64878, 3.0
    new_tp = MT5Connector._restore_tp_for_rr(price, sl, tp, True, rr, 5)
    adj_risk = abs(price - sl)
    assert new_tp == round(price + adj_risk * rr, 5)
    # New R:R is restored to ~3.0.
    assert abs((new_tp - price) / adj_risk - rr) < 1e-6
    # TP moved further away (more reward), never nearer.
    assert new_tp > price


def test_short_tp_extended_to_restore_rr():
    price, sl, tp, rr = 1.10000, 1.10060, 1.09820, 3.0
    new_tp = MT5Connector._restore_tp_for_rr(price, sl, tp, False, rr, 5)
    adj_risk = abs(price - sl)
    assert new_tp == round(price - adj_risk * rr, 5)
    assert abs((price - new_tp) / adj_risk - rr) < 1e-6
    assert new_tp < price


def test_tp_never_pulled_closer():
    # If the existing TP already exceeds the restored target, keep it (never
    # reduce reward).
    price, sl, rr = 1.10000, 1.10050, 3.0
    generous_tp = 1.10500  # already further than risk*rr
    new_tp = MT5Connector._restore_tp_for_rr(price, sl, generous_tp, True, rr, 5)
    assert new_tp == generous_tp


def test_zero_inputs_are_noops():
    assert MT5Connector._restore_tp_for_rr(1.1, 1.1, 1.105, True, 0.0, 5) == 1.105
    assert MT5Connector._restore_tp_for_rr(1.1, 1.1, 0.0, True, 3.0, 5) == 0.0
    # SL equal to price → zero risk → no change.
    assert MT5Connector._restore_tp_for_rr(1.1, 1.1, 1.105, True, 3.0, 5) == 1.105


def test_preserve_rr_can_be_disabled():
    c = MT5Connector(preserve_rr_after_adjust=False)
    assert c._preserve_rr_after_adjust is False
