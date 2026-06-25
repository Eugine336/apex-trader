"""
Tests for the deferred-batch fixes:
  #5 — restart-restore: plan linkage + tp3_hit + scale_in_count persist/round-trip
  #6 — dashboard surfacing: AccountRiskManager.snapshot(), GateTuner.all_offsets()
"""

import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

from adaptive.gate_tuner import GateTuner
from persistence.position_store import PositionStore
from risk.account_risk import AccountRiskManager


def _pos(**kw) -> SimpleNamespace:
    base = dict(
        order_id="OID1", platform="mt5", symbol="EURUSD", direction="BUY",
        lots=0.10, entry_price=1.1000, sl=1.0980, tp1=1.1040, tp2=1.1080,
        score=80, regime="trending", session="LONDON", entry_type="OB",
        open_time=datetime(2026, 6, 14, tzinfo=timezone.utc),
        tp1_hit=False, at_breakeven=False, trailing=False,
        tm_trade_id="T1", stake_usd=0.0, multiplier=100,
        idempotency_key="k", confluences=[], initial_risk_dollars=2.0,
        scale_in_count=1, plan_id="PLAN-9", plan_sl_pips=20.0,
        plan_scale_in_allowed=True, tp3_hit=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestPositionStoreRestartFields:
    def test_round_trip_persists_new_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PositionStore(os.path.join(tmp, "p.db"))
            store.save_position(_pos())
            rows = store.load_all_positions()
            store.close()
        assert len(rows) == 1
        row = rows[0]
        assert row["scale_in_count"] == 1
        assert row["plan_id"] == "PLAN-9"
        assert row["plan_sl_pips"] == 20.0
        assert row["plan_scale_in_allowed"] == 1
        assert row["tp3_hit"] == 1

    def test_update_tp3_hit_and_scale_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PositionStore(os.path.join(tmp, "p.db"))
            store.save_position(_pos(tp3_hit=False, scale_in_count=0))
            store.update_position("OID1", tp3_hit=True, scale_in_count=2)
            rows = store.load_all_positions()
            store.close()
        row = rows[0]
        assert row["tp3_hit"] == 1
        assert row["scale_in_count"] == 2


class TestAccountSnapshot:
    def test_snapshot_shape(self):
        am = AccountRiskManager(daily_loss_flatten_pct=5.0)
        am.update_balance("mt5:1", 1000.0)
        am.register_realized("mt5:1", -10.0)   # -1%
        am.update_unrealized("mt5:1", -5.0)    # -0.5% → combined -1.5%
        snap = am.snapshot()
        assert "mt5:1" in snap
        s = snap["mt5:1"]
        assert s["balance"] == 1000.0
        assert s["combined_pnl_pct"] == -1.5
        assert s["halted"] is False
        assert s["flatten_breached"] is False


class TestGateOffsets:
    def test_all_offsets_default_neutral(self):
        with tempfile.TemporaryDirectory() as tmp:
            gt = GateTuner(os.path.join(tmp, "gt.json"))
            offs = gt.all_offsets()
        # Every whitelisted tunable gate starts at a neutral (0.0) offset.
        assert offs == {f: 0.0 for f in GateTuner.TUNABLE}
