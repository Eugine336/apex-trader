"""Only outcomes produced by the canonical native thesis manager can train gates."""

from persistence.shadow_store import ShadowContract, ShadowResolution, ShadowStore
from cognition.identity import IDENTITY_VERSION


def _contract(cid: str, gate: str) -> ShadowContract:
    return ShadowContract(
        contract_id=cid, symbol="EURUSD", direction="BUY",
        entry_price=1.10, stop_loss=1.09, tp1=1.11, tp2=1.12,
        pip_size=0.0001, rejecting_gate=gate, ts_utc_ms=1,
    )


def _resolution(outcome: str, r: float, meta=None) -> ShadowResolution:
    return ShadowResolution(
        outcome=outcome, r_multiple=r, exit_reason="resolved",
        exit_price=1.11, resolution_ts=2,
        resolution_granularity="M5", bars_replayed=10,
        resolver_meta=meta,
    )


def test_gate_learning_excludes_legacy_and_unattributed_outcomes(tmp_path):
    store = ShadowStore(tmp_path / "shadow.db")
    store.insert_contract(_contract("native", "spread_gate"))
    store.insert_contract(_contract("legacy", "spread_gate"))
    store.insert_contract(_contract("unattributed", "spread_gate"))

    store.resolve_contract(
        "native",
        _resolution("WIN", 1.5, {
            "identity_version": IDENTITY_VERSION,
            "management_engine": "native_thesis",
            "learning_eligible": True,
        }),
    )
    store.resolve_contract(
        "legacy",
        _resolution("WIN", 3.0, {
            "identity_version": IDENTITY_VERSION,
            "management_engine": "legacy_trade_manager",
            "learning_eligible": False,
        }),
    )
    store.resolve_contract("unattributed", _resolution("LOSS", -1.0))

    assert store.get_outcomes_by_gate() == [{
        "rejecting_gate": "spread_gate",
        "outcome": "WIN",
        "cnt": 1,
        "avg_r": 1.5,
        "avg_bars": 10.0,
    }]
    store.close()


def test_wrong_identity_version_cannot_authorize_learning(tmp_path):
    store = ShadowStore(tmp_path / "shadow.db")
    store.insert_contract(_contract("wrong-version", "ev_gate"))
    store.resolve_contract(
        "wrong-version",
        _resolution("LOSS", -1.0, {
            "identity_version": "0.0.0",
            "management_engine": "native_thesis",
            "learning_eligible": True,
        }),
    )

    assert store.get_outcomes_by_gate() == []
    store.close()
