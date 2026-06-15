"""
Tests for scanner-stage rejection shadow contracts (PR 5).

Setups rejected at the scanner stage (OQ/EQ/score thresholds) never reach the
planner, so they were previously invisible to the shadow engine and gate
auto-tuner. We now synthesise an approximate trade (entry/SL/TP) for each so its
counterfactual outcome can be resolved, marked ``source='scanner_approximation'``
to distinguish it from planner-derived contracts.
"""

import tempfile
from pathlib import Path
from types import SimpleNamespace

from adaptive.gate_tuner import GateTuner
from persistence.shadow_store import ShadowContract, ShadowResolution, ShadowStore
from scanner.pair_scanner import RejectedSetup, build_rejected_setup


# ── build_rejected_setup: approximation geometry ─────────────────────────────

def test_build_rejected_setup_long():
    rs = build_rejected_setup(
        symbol="EURUSD",
        direction="LONG",
        rejecting_gate="oq_threshold:4.0<5.0",
        current_price=1.1000,
        atr_pips=10.0,
        pip_size=0.0001,
        scan_timestamp=123.0,
        ob_midpoint=None,
        oq=4.0,
        eq=6.0,
        score=70,
    )
    assert rs is not None
    # entry falls back to current_price; atr_price = 10 * 0.0001 = 0.001
    assert rs.approximate_entry == 1.1000
    assert abs(rs.approximate_sl - (1.1000 - 1.5 * 0.001)) < 1e-9
    assert abs(rs.approximate_tp - (1.1000 + 2.0 * 0.001)) < 1e-9
    assert rs.rejecting_gate == "oq_threshold:4.0<5.0"
    assert rs.atr == 10.0
    assert rs.oq == 4.0 and rs.eq == 6.0 and rs.score == 70


def test_build_rejected_setup_short_uses_ob_midpoint():
    rs = build_rejected_setup(
        symbol="EURUSD",
        direction="SHORT",
        rejecting_gate="eq_threshold:3.0<5.0",
        current_price=1.2000,
        atr_pips=20.0,
        pip_size=0.0001,
        scan_timestamp=1.0,
        ob_midpoint=1.2050,
    )
    assert rs is not None
    # entry uses OB midpoint; SHORT → SL above, TP below; atr_price = 0.002
    assert rs.approximate_entry == 1.2050
    assert abs(rs.approximate_sl - (1.2050 + 1.5 * 0.002)) < 1e-9
    assert abs(rs.approximate_tp - (1.2050 - 2.0 * 0.002)) < 1e-9


def test_build_rejected_setup_missing_atr_returns_none():
    assert build_rejected_setup(
        symbol="EURUSD", direction="LONG", rejecting_gate="oq_threshold:x",
        current_price=1.10, atr_pips=None, pip_size=0.0001, scan_timestamp=1.0,
    ) is None
    assert build_rejected_setup(
        symbol="EURUSD", direction="LONG", rejecting_gate="oq_threshold:x",
        current_price=1.10, atr_pips=0.0, pip_size=0.0001, scan_timestamp=1.0,
    ) is None


def test_build_rejected_setup_non_directional_returns_none():
    assert build_rejected_setup(
        symbol="EURUSD", direction="NEUTRAL", rejecting_gate="oq_threshold:x",
        current_price=1.10, atr_pips=10.0, pip_size=0.0001, scan_timestamp=1.0,
    ) is None


# ── ShadowStore: source marker round-trips ───────────────────────────────────

def test_shadow_store_source_defaults_to_planner():
    with tempfile.TemporaryDirectory() as tmp:
        s = ShadowStore(Path(tmp) / "shadow.db")
        s.insert_contract(ShadowContract(
            contract_id="p1", symbol="EURUSD", direction="LONG",
            entry_price=1.10, stop_loss=1.09, tp1=1.11, tp2=1.12,
            pip_size=0.0001, rejecting_gate="planner:SKIP", ts_utc_ms=1,
        ))
        rows = s.get_all_contracts(rejecting_gate="planner:SKIP")
        assert rows and rows[0].source == "planner"


def test_shadow_store_persists_scanner_source():
    with tempfile.TemporaryDirectory() as tmp:
        s = ShadowStore(Path(tmp) / "shadow.db")
        s.insert_contract(ShadowContract(
            contract_id="s1", symbol="EURUSD", direction="LONG",
            entry_price=1.10, stop_loss=1.0985, tp1=1.1020, tp2=1.1020,
            pip_size=0.0001, rejecting_gate="oq_threshold:4.0<5.0", ts_utc_ms=1,
            source="scanner_approximation",
        ))
        rows = s.get_all_contracts(rejecting_gate="oq_threshold:4.0<5.0")
        assert rows and rows[0].source == "scanner_approximation"


# ── GateTuner / outcomes-by-gate visibility ──────────────────────────────────

def _resolution(outcome: str, r: float) -> ShadowResolution:
    return ShadowResolution(
        outcome=outcome, r_multiple=r, exit_reason="tp1", exit_price=1.11,
        resolution_ts=2, resolution_granularity="M5", bars_replayed=10,
    )


def test_scanner_gate_families_appear_in_outcomes_by_gate():
    with tempfile.TemporaryDirectory() as tmp:
        s = ShadowStore(Path(tmp) / "shadow.db")
        for cid, gate in (
            ("a1", "oq_threshold:4.0<5.0"),
            ("b1", "eq_threshold:3.0<5.0"),
            ("c1", "score_threshold:50<65"),
        ):
            s.insert_contract(ShadowContract(
                contract_id=cid, symbol="EURUSD", direction="LONG",
                entry_price=1.10, stop_loss=1.09, tp1=1.11, tp2=1.12,
                pip_size=0.0001, rejecting_gate=gate, ts_utc_ms=1,
                source="scanner_approximation",
            ))
            s.resolve_contract(cid, _resolution("WIN", 1.0))
        gates = {row["rejecting_gate"] for row in s.get_outcomes_by_gate()}
        assert "oq_threshold:4.0<5.0" in gates
        assert "eq_threshold:3.0<5.0" in gates
        assert "score_threshold:50<65" in gates


def test_scanner_gate_families_not_auto_tuned():
    """Quality gates are reported but NEVER auto-tuned (safety-by-default)."""
    for family in ("oq_threshold", "eq_threshold", "score_threshold"):
        assert family not in GateTuner.TUNABLE
    with tempfile.TemporaryDirectory() as tmp:
        gt = GateTuner(str(Path(tmp) / "g.json"))
        rows = [{"rejecting_gate": "oq_threshold:4.0<5.0", "outcome": "WIN",
                 "cnt": 100, "avg_r": 1.0}]
        assert gt.calibrate(rows) == []


# ── Main-loop persistence wiring ─────────────────────────────────────────────

def test_persist_scanner_rejections_inserts_contracts():
    from platforms.main_loop import TradingLoop

    with tempfile.TemporaryDirectory() as tmp:
        store = ShadowStore(Path(tmp) / "shadow.db")
        loop = TradingLoop.__new__(TradingLoop)
        loop._shadow_store = store
        loop._current_cycle_id = None

        report = SimpleNamespace(rejected_setups=[
            RejectedSetup(
                symbol="EURUSD", direction="LONG",
                rejecting_gate="oq_threshold:4.0<5.0",
                approximate_entry=1.10, approximate_sl=1.0985,
                approximate_tp=1.1020, atr=10.0, scan_timestamp=1.0,
                oq=4.0, eq=6.0, score=70,
            ),
        ])
        TradingLoop._persist_scanner_rejections(loop, report)

        rows = store.get_all_contracts(rejecting_gate="oq_threshold:4.0<5.0")
        assert len(rows) == 1
        assert rows[0].source == "scanner_approximation"
        assert rows[0].symbol == "EURUSD"
        assert rows[0].direction == "LONG"


def test_persist_scanner_rejections_empty_is_noop():
    from platforms.main_loop import TradingLoop

    with tempfile.TemporaryDirectory() as tmp:
        store = ShadowStore(Path(tmp) / "shadow.db")
        loop = TradingLoop.__new__(TradingLoop)
        loop._shadow_store = store
        loop._current_cycle_id = None
        # No rejected_setups attribute / empty → must not raise.
        TradingLoop._persist_scanner_rejections(loop, SimpleNamespace(rejected_setups=[]))
        TradingLoop._persist_scanner_rejections(loop, SimpleNamespace())
        assert store.count_by_status().get("PENDING", 0) == 0
