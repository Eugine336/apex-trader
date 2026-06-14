"""
Test for rejection-by-gate analytics (B) — ShadowStore.get_gate_edge().

Surfaces, per rejecting gate, how many REJECTED setups would have won vs lost
and the EV (count-weighted mean R) of having taken them. EV is the decision-
relevant number: a gate rejecting net-profitable setups (ev_r > 0) is too
strict; one filtering losers (ev_r < 0) is earning its keep. EXPIRED setups are
counted in `total` but excluded from EV/win-rate (no realized R).
"""

import tempfile
from pathlib import Path

from persistence.shadow_store import ShadowContract, ShadowResolution, ShadowStore


def _contract(cid: str, gate: str) -> ShadowContract:
    return ShadowContract(
        contract_id=cid, symbol="EURUSD", direction="BUY",
        entry_price=1.10, stop_loss=1.09, tp1=1.11, tp2=1.12,
        pip_size=0.0001, rejecting_gate=gate, ts_utc_ms=1,
    )


def _resolution(outcome: str, r: float) -> ShadowResolution:
    return ShadowResolution(
        outcome=outcome, r_multiple=r, exit_reason="tp1", exit_price=1.11,
        resolution_ts=2, resolution_granularity="M5", bars_replayed=10,
    )


def test_gate_edge_ev_is_count_weighted():
    with tempfile.TemporaryDirectory() as tmp:
        s = ShadowStore(Path(tmp) / "shadow.db")
        # ev_gate rejected 3 setups: 2 wins (+2.0, +1.0) and 1 loss (-1.0).
        s.insert_contract(_contract("c1", "ev_gate:x"))
        s.insert_contract(_contract("c2", "ev_gate:x"))
        s.insert_contract(_contract("c3", "ev_gate:x"))
        s.resolve_contract("c1", _resolution("WIN", 2.0))
        s.resolve_contract("c2", _resolution("WIN", 1.0))
        s.resolve_contract("c3", _resolution("LOSS", -1.0))
        edge = {g["gate"]: g for g in s.get_gate_edge()}

    g = edge["ev_gate:x"]
    assert g["total"] == 3
    assert g["wins"] == 2
    assert g["losses"] == 1
    assert g["decisive"] == 3
    # EV = (2.0 + 1.0 - 1.0) / 3 = 0.667 — this gate is rejecting winners.
    assert g["ev_r"] == 0.667
    assert g["win_rate"] == 66.7


def test_gate_edge_excludes_expired_from_ev():
    with tempfile.TemporaryDirectory() as tmp:
        s = ShadowStore(Path(tmp) / "shadow.db")
        s.insert_contract(_contract("w1", "spread:2.0"))
        s.insert_contract(_contract("e1", "spread:2.0"))
        s.resolve_contract("w1", _resolution("WIN", 1.0))
        s.mark_expired("e1", bars_replayed=99)  # EXPIRED → no realized R
        edge = {g["gate"]: g for g in s.get_gate_edge()}

    g = edge["spread:2.0"]
    assert g["total"] == 2          # expired still counted as rejected
    assert g["expired"] == 1
    assert g["wins"] == 1
    assert g["decisive"] == 1       # expired excluded from decisive
    assert g["ev_r"] == 1.0         # EV over the one resolved setup only
    assert g["win_rate"] == 100.0


def test_gate_edge_negative_ev_gate_is_earning_its_keep():
    with tempfile.TemporaryDirectory() as tmp:
        s = ShadowStore(Path(tmp) / "shadow.db")
        s.insert_contract(_contract("l1", "news:HIGH"))
        s.insert_contract(_contract("l2", "news:HIGH"))
        s.resolve_contract("l1", _resolution("LOSS", -1.0))
        s.resolve_contract("l2", _resolution("LOSS", -0.5))
        edge = {g["gate"]: g for g in s.get_gate_edge()}

    g = edge["news:HIGH"]
    assert g["ev_r"] == -0.75       # negative EV ⇒ gate correctly filters losers
    assert g["win_rate"] == 0.0


def test_gate_edge_empty_when_unresolved():
    with tempfile.TemporaryDirectory() as tmp:
        s = ShadowStore(Path(tmp) / "shadow.db")
        s.insert_contract(_contract("p1", "ev_gate:x"))  # stays PENDING
        assert s.get_gate_edge() == []
