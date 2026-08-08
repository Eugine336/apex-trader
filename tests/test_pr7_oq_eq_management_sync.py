"""
APEX TRADER — PR 7 regression tests.

P3 — Live OQ/EQ re-validation during management:
  * DecisionEngine adds bounded CLOSE/TIGHTEN pressure when the live OQ/EQ
    (recomputed mid-trade) decay below their floors, and the decay reasons
    surface in the chosen verdict's evidence.
  * The pressure is inert when the live scores are unavailable (None) or the
    feature is disabled — so the entry path and legacy contexts are unaffected.

P4 — Sync the two management brains:
  * The mechanical TradeManager stores the strategic (multi-timeframe)
    structure read and defers its independent M5 structure-exit while that
    read is fresh and intact.
  * A stale (>max-age) or absent strategic read falls back to the M5 analysis.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd

from decision.context import TradeContext
from decision.engine import DecisionEngine
from decision.actions import Action
from decision.situation import SituationAssessment
from management.trade_manager import (
    EntrySignal as TMEntrySignal,
    TradeManager,
    TradeStatus,
)
import management.trade_manager as tm_module
from brain.structure_engine import StructureEvent


# ── Helpers ────────────────────────────────────────────────────────────────

def _ctx(**overrides) -> TradeContext:
    defaults = dict(
        symbol="EURUSD",
        order_id="oid-1",
        direction="BUY",
        entry_type="APEX_ENTRY",
        entry_price=1.0850,
        current_price=1.0860,
        current_sl=1.0820,
        pnl_pips=10.0,
        hold_minutes=45.0,
        original_risk_pips=30.0,
        scan_score=60,
        scan_direction="LONG",
    )
    defaults.update(overrides)
    return TradeContext(**defaults)


def _sa(**overrides) -> SituationAssessment:
    sa = SituationAssessment()
    for k, v in overrides.items():
        setattr(sa, k, v)
    return sa


def _signal(direction: str = "LONG", **overrides) -> TMEntrySignal:
    defaults = dict(
        pair="EURUSD",
        direction=direction,
        entry_price=1.10000,
        stop_loss=1.09800,
        tp1=1.10200,
        tp2=1.10400,
        risk_reward_1=1.0,
        risk_reward_2=2.0,
        position_size_lots=0.50,
        score=90,
    )
    defaults.update(overrides)
    return TMEntrySignal(**defaults)


def _m5_df(rows: int = 14) -> pd.DataFrame:
    data = []
    for i in range(rows):
        o = 1.10000 + i * 0.00010
        data.append({
            "time": pd.Timestamp("2025-01-01") + pd.Timedelta(minutes=5 * i),
            "open": o, "high": o + 0.0002, "low": o - 0.0002,
            "close": o + 0.0001, "volume": 100,
        })
    return pd.DataFrame(data)


class _FakeStructureEngine:
    """Deterministic StructureEngine stand-in for management tests."""

    _event = StructureEvent.CHOCH_BEARISH

    def __init__(self, *args, **kwargs):
        pass

    def analyze(self, df):
        class _A:
            last_event = _FakeStructureEngine._event
        return _A()


# ── P3: OQ/EQ decay pressure ────────────────────────────────────────────────

class TestOqEqDecayPressure:
    def setup_method(self):
        self.eng = DecisionEngine(oq_floor=5.0, eq_floor=5.0, oq_decay_significant=2.0)

    def test_inert_when_live_scores_absent(self):
        close, tighten, reasons = self.eng._oq_eq_decay_pressure(_ctx())
        assert close == 0.0 and tighten == 0.0 and reasons == []

    def test_inert_when_disabled(self):
        eng = DecisionEngine(oq_eq_decay_enabled=False)
        close, tighten, reasons = eng._oq_eq_decay_pressure(
            _ctx(live_oq=0.0, live_eq=0.0)
        )
        assert close == 0.0 and tighten == 0.0 and reasons == []

    def test_oq_below_floor_adds_close_and_tighten(self):
        close, tighten, reasons = self.eng._oq_eq_decay_pressure(
            _ctx(live_oq=2.0, live_eq=8.0)
        )
        assert close > 0.0
        assert tighten > 0.0
        assert any("OQ collapsed" in r for r in reasons)

    def test_eq_below_floor_adds_tighten_only(self):
        close, tighten, reasons = self.eng._oq_eq_decay_pressure(
            _ctx(live_oq=8.0, live_eq=2.0)
        )
        assert close == 0.0
        assert tighten > 0.0
        assert any("EQ degraded" in r for r in reasons)

    def test_significant_decay_above_floor_adds_tighten(self):
        # OQ still above floor (6.0) but dropped 3.0 since entry (> 2.0 sig).
        close, tighten, reasons = self.eng._oq_eq_decay_pressure(
            _ctx(live_oq=6.0, live_eq=8.0, oq_decay=3.0)
        )
        assert close == 0.0
        assert tighten > 0.0
        assert any("OQ decayed" in r for r in reasons)

    def test_no_pressure_when_quality_healthy(self):
        close, tighten, reasons = self.eng._oq_eq_decay_pressure(
            _ctx(live_oq=8.0, live_eq=8.0, oq_decay=0.5)
        )
        assert close == 0.0 and tighten == 0.0 and reasons == []


class TestOqEqDecayInDecideManagement:
    def setup_method(self):
        self.eng = DecisionEngine()

    def test_oq_collapse_reason_in_close_verdict(self):
        # Strong existing CLOSE lean (broken structure + loss against trend).
        sa = _sa(
            structure_integrity=0.1,
            momentum=-0.5,
            tf_alignment=-0.5,
            profit_state=-1.0,
        )
        ctx = _ctx(live_oq=1.0, live_eq=8.0, entry_oq=7.0, oq_decay=6.0)
        decision = self.eng.decide_management(ctx, sa)
        assert decision.action == Action.CLOSE
        assert any("OQ collapsed" in e for e in decision.evidence)

    def test_eq_degraded_reason_in_tighten_verdict(self):
        # Profit + fading momentum → TIGHTEN base; EQ degraded reinforces it.
        sa = _sa(profit_state=2.0, momentum=-0.5, tf_alignment=0.0)
        ctx = _ctx(live_oq=8.0, live_eq=2.0, entry_eq=7.0, eq_decay=5.0)
        decision = self.eng.decide_management(ctx, sa)
        assert decision.action == Action.TIGHTEN_SL
        assert any("EQ degraded" in e for e in decision.evidence)

    def test_no_quality_reason_when_live_scores_absent(self):
        sa = _sa(structure_integrity=0.1, momentum=-0.5, tf_alignment=-0.5, profit_state=-1.0)
        decision = self.eng.decide_management(_ctx(), sa)
        assert "OQ" not in decision.reason
        assert "EQ" not in decision.reason


# ── P3: entry OQ/EQ carried onto the managed trade ──────────────────────────

class TestEntryQualityCarried:
    def test_open_trade_stores_entry_oq_eq(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal(entry_oq=7.4, entry_eq=6.2))
        assert trade.entry_oq == 7.4
        assert trade.entry_eq == 6.2

    def test_open_trade_entry_quality_defaults_none(self):
        tm = TradeManager()
        trade = tm.open_trade(_signal())
        assert trade.entry_oq is None
        assert trade.entry_eq is None


# ── P4: strategic-structure sync ────────────────────────────────────────────

class TestStrategicStructureSync:
    def setup_method(self):
        self.tm = TradeManager(strategic_structure_max_age_seconds=600.0)
        self.trade = self.tm.open_trade(_signal())
        # Older than the 30-min structure-exit floor so the M5 read is eligible.
        self.trade.entry_time = datetime.now(timezone.utc) - timedelta(minutes=45)

    def test_record_strategic_assessment_stores_fields(self):
        now = datetime.now(timezone.utc)
        self.tm.record_strategic_assessment(self.trade, 0.8, 0.4, assessed_at=now)
        assert self.trade.strategic_structure_integrity == 0.8
        assert self.trade.strategic_tf_alignment == 0.4
        assert self.trade.strategic_assessment_time == now

    def test_fresh_assessment_returns_tuple(self):
        now = datetime.now(timezone.utc)
        self.tm.record_strategic_assessment(self.trade, 0.7, 0.2, assessed_at=now)
        result = self.tm._fresh_strategic_assessment(self.trade, now)
        assert result == (0.7, 0.2)

    def test_stale_assessment_returns_none(self):
        stale = datetime.now(timezone.utc) - timedelta(seconds=900)
        self.tm.record_strategic_assessment(self.trade, 0.7, 0.2, assessed_at=stale)
        result = self.tm._fresh_strategic_assessment(
            self.trade, datetime.now(timezone.utc)
        )
        assert result is None

    def test_absent_assessment_returns_none(self):
        assert self.tm._fresh_strategic_assessment(
            self.trade, datetime.now(timezone.utc)
        ) is None

    def test_structure_exit_deferred_when_strategic_intact(self, monkeypatch):
        monkeypatch.setattr(tm_module, "StructureEngine", _FakeStructureEngine)
        now = datetime.now(timezone.utc)
        # Strategic brain says structure intact (≥ 0.6) and the read is fresh.
        self.tm.record_strategic_assessment(self.trade, 0.85, 0.5, assessed_at=now)
        fired = self.tm._check_structure_exit(self.trade, _m5_df(), bar_time=now)
        assert fired is False
        assert self.trade.status == TradeStatus.OPEN

    def test_structure_exit_fires_when_strategic_stale(self, monkeypatch):
        monkeypatch.setattr(tm_module, "StructureEngine", _FakeStructureEngine)
        now = datetime.now(timezone.utc)
        stale = now - timedelta(seconds=900)
        # Intact, but stale → must fall back to the mechanical M5 read (closes).
        self.tm.record_strategic_assessment(self.trade, 0.85, 0.5, assessed_at=stale)
        fired = self.tm._check_structure_exit(self.trade, _m5_df(), bar_time=now)
        assert fired is True
        assert self.trade.status == TradeStatus.TIME_EXIT

    def test_structure_exit_fires_when_no_strategic_read(self, monkeypatch):
        monkeypatch.setattr(tm_module, "StructureEngine", _FakeStructureEngine)
        now = datetime.now(timezone.utc)
        fired = self.tm._check_structure_exit(self.trade, _m5_df(), bar_time=now)
        assert fired is True
        assert self.trade.status == TradeStatus.TIME_EXIT

    def test_broken_strategic_read_confirms_exit_reason(self, monkeypatch):
        monkeypatch.setattr(tm_module, "StructureEngine", _FakeStructureEngine)
        now = datetime.now(timezone.utc)
        # Fresh strategic read that says structure broken (< intact threshold).
        self.tm.record_strategic_assessment(self.trade, 0.2, -0.5, assessed_at=now)
        fired = self.tm._check_structure_exit(self.trade, _m5_df(), bar_time=now)
        assert fired is True
        assert "strategic structure confirms break" in (self.trade.close_reason or "")
