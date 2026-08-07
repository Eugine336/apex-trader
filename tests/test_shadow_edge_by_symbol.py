"""
Test for #9 (part 2) — rejected-setup shadow edge grouped by symbol.

This data is surfaced for operator visibility only. It is deliberately NOT fed
into the pair/regime/session learners: rejected-setup outcomes measure setups
the bot DECLINED, not the pairs it trades, so feeding them would wrongly
penalise good pairs. The sound automated consumer is the gate auto-tuner.
"""

import tempfile
from pathlib import Path

from persistence.shadow_store import ShadowContract, ShadowResolution, ShadowStore


def _contract(cid: str, symbol: str) -> ShadowContract:
    return ShadowContract(
        contract_id=cid, symbol=symbol, direction="BUY",
        entry_price=1.10, stop_loss=1.09, tp1=1.11, tp2=1.12,
        pip_size=0.0001, rejecting_gate="ev_gate:x", ts_utc_ms=1,
    )


def _resolution(outcome: str, r: float) -> ShadowResolution:
    return ShadowResolution(
        outcome=outcome, r_multiple=r, exit_reason="tp1", exit_price=1.11,
        resolution_ts=2, resolution_granularity="M5", bars_replayed=10,
    )


def test_outcomes_grouped_by_symbol():
    with tempfile.TemporaryDirectory() as tmp:
        s = ShadowStore(Path(tmp) / "shadow.db")
        s.insert_contract(_contract("c1", "EURUSD"))
        s.insert_contract(_contract("c2", "EURUSD"))
        s.insert_contract(_contract("c3", "GBPUSD"))
        s.resolve_contract("c1", _resolution("WIN", 1.5))
        s.resolve_contract("c2", _resolution("LOSS", -1.0))
        s.resolve_contract("c3", _resolution("WIN", 2.0))
        rows = s.get_outcomes_by_symbol()

    by = {(r["symbol"], r["outcome"]): r for r in rows}
    assert by[("EURUSD", "WIN")]["cnt"] == 1
    assert by[("EURUSD", "LOSS")]["cnt"] == 1
    assert by[("GBPUSD", "WIN")]["cnt"] == 1


def test_only_resolved_counted():
    with tempfile.TemporaryDirectory() as tmp:
        s = ShadowStore(Path(tmp) / "shadow.db")
        s.insert_contract(_contract("p1", "EURUSD"))  # stays PENDING
        assert s.get_outcomes_by_symbol() == []
