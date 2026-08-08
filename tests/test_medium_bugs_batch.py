"""Regression tests for the MEDIUM-severity audit batch.

Covers the fixes for:
  #19 — micro-account min-lot risk ceiling consistency with the engine cap
  #22 — ATR look-ahead (bfill) removed in favour of a causal expanding mean
  #28 — WorldModel snapshot isolated from post-publish OrderBlock mutation

These modules require pandas + loguru, so the suite runs in the full
environment (the sandbox used to author the fix lacks those deps).
"""

from __future__ import annotations

import pandas as pd


# ── #22 — ATR causal warm-up (no look-ahead backfill) ───────────────────────
class TestATRNoLookahead:
    def _rising_ohlc(self, n: int) -> pd.DataFrame:
        # Strictly widening ranges so the rolling mean is monotonically rising:
        # this makes the warm-up value provably different from the first
        # full-window value (a backfill would have made them equal).
        highs = [1.0 + i * 0.01 for i in range(n)]
        lows = [1.0 - i * 0.01 for i in range(n)]
        closes = [1.0 for _ in range(n)]
        return pd.DataFrame({"high": highs, "low": lows, "close": closes})

    def test_series_fully_populated(self) -> None:
        from brain.volatility_stop import atr_series

        df = self._rising_ohlc(30)
        s = atr_series(df, period=14)
        assert len(s) == len(df)
        assert not s.isna().any()

    def test_warmup_is_causal_not_backfilled(self) -> None:
        from brain.volatility_stop import atr_series

        df = self._rising_ohlc(30)
        s = atr_series(df, period=14)
        # A backward fill would have copied the first full-window value into the
        # warm-up rows, making iloc[0] == iloc[13]. The causal expanding mean
        # keeps them distinct on rising data.
        assert s.iloc[0] != s.iloc[13]


# ── #19 — micro-account ceiling honours the engine cap above the threshold ───
class TestMicroAccountRiskCeiling:
    def test_non_micro_min_lot_capped_at_engine_cap(self) -> None:
        from risk.position_sizer import PositionSizer

        sizer = PositionSizer()  # micro<100, max_risk 5%, engine_cap 2.5%
        # $300 account is ABOVE the micro threshold. A min-lot max_loss of $10
        # is 3.33% — inside the 5% micro tolerance but ABOVE the 2.5% engine
        # cap, so it must be rejected (no over-sizing on a normal account).
        lots, mode = sizer._adjust_for_account_size(
            lots=0.01, account_balance=300.0, risk_amount=0.5, max_loss=10.0,
        )
        assert lots == 0.0
        assert mode == "skip_min_lot_over_risk"

    def test_micro_account_still_tolerates_up_to_5pct(self) -> None:
        from risk.position_sizer import PositionSizer

        sizer = PositionSizer()
        # $80 account is a genuine micro account: the min-lot tolerance (5%)
        # still applies so a 3.75% min-lot risk is allowed (it cannot trade
        # smaller than 0.01 lots).
        lots, mode = sizer._adjust_for_account_size(
            lots=0.01, account_balance=80.0, risk_amount=0.5, max_loss=3.0,
        )
        assert lots == 0.01
        assert mode == "lots"


# ── #28 — WorldModel snapshot isolated from later OrderBlock mutation ────────
class TestWorldModelImmutability:
    def test_orderblock_mutation_after_publish_does_not_leak(self) -> None:
        from brain.order_block import OrderBlock, OBStatus
        from brain.world_model import build_world_model

        ob = OrderBlock(
            kind="BULLISH", top=1.1000, bottom=1.0900, midpoint=1.0950,
            origin_index=5, strength="STRONG", status=OBStatus.FRESH,
            impulse_size=20.0, timestamp=pd.Timestamp.utcnow(), timeframe="H1",
            breaker=False,
        )
        wm = build_world_model(
            symbol="EURUSD", version=1, order_blocks={"H1": [ob]},
        )
        # Producer mutates its own object AFTER the snapshot was published.
        ob.status = OBStatus.BROKEN
        snap_ob = wm.order_blocks_by_tf()["H1"][0]
        assert snap_ob.status == OBStatus.FRESH
