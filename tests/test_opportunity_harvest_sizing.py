"""Opportunity-harvesting sizing opt-in (Constitution Part V).

When ``allow_min_lot_over_risk`` is set, an entry whose unavoidable broker
minimum lot risks more than the per-trade ceiling is taken at the minimum lot
instead of being rejected — so a small account can still participate in the
opportunities the Brain authorises.
"""

from risk.position_sizer import PositionSizer


def test_over_ceiling_min_lot_rejected_by_default():
    sizer = PositionSizer()  # allow_min_lot_over_risk defaults False
    # $10 account, min-lot max_loss $8 = 80% risk — far above any ceiling.
    lots, mode = sizer._adjust_for_account_size(
        lots=0.01, account_balance=10.0, risk_amount=0.5, max_loss=8.0,
    )
    assert lots == 0.0
    assert mode == "lots_skip_micro"


def test_over_ceiling_min_lot_taken_when_opted_in():
    sizer = PositionSizer(allow_min_lot_over_risk=True)
    lots, mode = sizer._adjust_for_account_size(
        lots=0.01, account_balance=10.0, risk_amount=0.5, max_loss=8.0,
    )
    assert lots == 0.01
    assert mode == "lots_min_lot_over_risk_opt_in"


def test_opt_in_does_not_change_within_ceiling_behaviour():
    # A min-lot risk already inside the ceiling is unaffected by the opt-in.
    sizer = PositionSizer(allow_min_lot_over_risk=True)
    lots, mode = sizer._adjust_for_account_size(
        lots=0.01, account_balance=80.0, risk_amount=0.5, max_loss=3.0,
    )
    assert lots == 0.01
    assert mode == "lots"


def test_opt_in_still_respects_on_target_shortcut():
    # When the size is essentially on-target (<=1.5x risk) nothing changes.
    sizer = PositionSizer(allow_min_lot_over_risk=True)
    lots, mode = sizer._adjust_for_account_size(
        lots=0.02, account_balance=1000.0, risk_amount=10.0, max_loss=12.0,
    )
    assert lots == 0.02
    assert mode == "lots"
