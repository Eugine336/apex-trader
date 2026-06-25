"""Tests for the EV gate — Phase 4 Opportunity Engine.

The expected-value gate replaces the alignment-floor gate as the entry plane's
directional check. EV = p_win × R:R − p_loss (in R-multiples), where p_win /
p_loss are the Phase 3 long/short probabilities mapped to the trade direction.
Counter-trend trades (p_loss > p_win) must clear a higher EV bar.
"""

from datetime import datetime, timedelta, timezone

from brain.fvg_detector import FairValueGap, FVGStatus
from brain.order_block import OrderBlock, OBStatus
from brain.structure_engine import StructureAnalysis, StructureEvent, Trend
from brain.world_model import WorldModelStore, build_world_model
from entry.entry_gate import EntryGate
from entry.models import EntryConfig, EntryZone, ZoneType
from entry.zone_watcher import extract_entry_zones


def _ts() -> datetime:
    return datetime.now(timezone.utc)


def _make_zone(expires_in_seconds=600) -> EntryZone:
    now = datetime.now(timezone.utc)
    return EntryZone(
        symbol="EURUSD", direction="LONG", zone_type=ZoneType.FVG_MIDPOINT,
        top=102.0, bottom=99.5, midpoint=100.0,
        invalidation_level=99.0, conviction=85,
        created_at=now, expires_at=now + timedelta(seconds=expires_in_seconds),
        timeframe="M5",
    )


def _defaults(**overrides):
    """Valid default kwargs for validate_all (LONG, risk=1, rr=2 via tp1=102)."""
    base = dict(
        symbol="EURUSD", direction="LONG",
        entry_price=100.0, stop_loss=99.0,
        tp1=102.0, tp2=104.0,
        score=85, current_spread_pips=1.0,
        zone=_make_zone(),
        is_instrument_known=True, is_market_open=True,
        is_session_active=True, is_news_clear=True,
        is_drawdown_ok=True,
        long_probability=0.6, short_probability=0.4,
    )
    base.update(overrides)
    return base


# ── Core EV computation ──────────────────────────────────────────────────────


class TestEvComputation:
    def test_positive_ev_with_trend_passes(self):
        # p_win=0.6, rr=2.0, p_loss=0.4 → EV = 0.6*2 - 0.4 = 0.8R > 0.3R.
        gate = EntryGate()
        r = gate._check_ev("EURUSD", "LONG", 100.0, 99.0, 102.0, 0.6, 0.4)
        assert r.passed is True

    def test_negative_ev_rejects(self):
        # p_win=0.2, rr=1.0, p_loss=0.8 → EV = 0.2 - 0.8 = -0.6R; counter-trend
        # bar 0.4R → reject.
        gate = EntryGate()
        r = gate._check_ev("EURUSD", "LONG", 100.0, 99.0, 101.0, 0.2, 0.8)
        assert r.passed is False

    def test_counter_trend_needs_premium(self):
        # p_win=0.4 (counter), rr=2.0, p_loss=0.6 → EV = 0.8 - 0.6 = 0.2R.
        # Counter-trend bar = 0.3 + 0.1 = 0.4R → reject.
        gate = EntryGate()
        r = gate._check_ev("EURUSD", "LONG", 100.0, 99.0, 102.0, 0.4, 0.6)
        assert r.passed is False
        assert "counter-trend" in r.reason

    def test_counter_trend_high_rr_passes(self):
        # p_win=0.4 (counter), rr=3.0, p_loss=0.6 → EV = 1.2 - 0.6 = 0.6R > 0.4R.
        gate = EntryGate()
        r = gate._check_ev("EURUSD", "LONG", 100.0, 99.0, 103.0, 0.4, 0.6)
        assert r.passed is True

    def test_zero_probability_rejects(self):
        # No probabilistic edge → EV = 0 < 0.3R → reject (no edge, no trade).
        gate = EntryGate()
        r = gate._check_ev("EURUSD", "LONG", 100.0, 99.0, 102.0, 0.0, 0.0)
        assert r.passed is False

    def test_zero_risk_rejects(self):
        gate = EntryGate()
        r = gate._check_ev("EURUSD", "LONG", 100.0, 100.0, 102.0, 0.6, 0.1)
        assert r.passed is False
        assert "Zero risk distance" in r.reason


# ── Disabled flag falls back to the legacy alignment gate ────────────────────


class TestEvGateDisabledFallback:
    def test_ev_gate_disabled_falls_back(self):
        cfg = EntryConfig(ev_gate_enabled=False)
        gate = EntryGate(config=cfg)
        _, results = gate.validate_all(**_defaults(alignment=-0.93))
        names = {r.gate_name for r in results}
        assert "alignment" in names
        assert "ev_gate" not in names
        # Strongly counter-trend alignment is rejected by the legacy gate.
        align = next(r for r in results if r.gate_name == "alignment")
        assert align.passed is False

    def test_ev_gate_enabled_replaces_alignment(self):
        gate = EntryGate()  # ev_gate_enabled defaults True
        _, results = gate.validate_all(**_defaults())
        names = {r.gate_name for r in results}
        assert "ev_gate" in names
        assert "alignment" not in names


# ── Reason string / labels ───────────────────────────────────────────────────


class TestEvGateReason:
    def test_ev_gate_reason_includes_details(self):
        gate = EntryGate()
        r = gate._check_ev("EURUSD", "LONG", 100.0, 99.0, 102.0, 0.6, 0.4)
        assert "EV" in r.reason
        assert "p_win=" in r.reason
        assert "rr=" in r.reason

    def test_counter_trend_label(self):
        # p_loss (short) > p_win (long) for a LONG trade → counter-trend.
        gate = EntryGate()
        r = gate._check_ev("EURUSD", "LONG", 100.0, 99.0, 102.0, 0.3, 0.7)
        assert "counter-trend" in r.reason

    def test_with_trend_label(self):
        gate = EntryGate()
        r = gate._check_ev("EURUSD", "LONG", 100.0, 99.0, 102.0, 0.7, 0.3)
        assert "with-trend" in r.reason


# ── GateTuner loosening ──────────────────────────────────────────────────────


class _FakeTuner:
    def __init__(self, offset: float):
        self._offset = offset

    def offset(self, family: str) -> float:
        return self._offset if family == "ev_gate" else 0.0


class TestEvGateTuner:
    def test_gate_tuner_loosens_ev(self):
        # EV = 0.45*1 - 0.2 = 0.25R (with-trend) → below base 0.30 bar.
        base = EntryGate()
        r_base = base._check_ev("EURUSD", "LONG", 100.0, 99.0, 101.0, 0.45, 0.2)
        assert r_base.passed is False
        # A -0.10 loosening offset lowers the bar to 0.20R → 0.25R now passes.
        tuned = EntryGate(gate_tuner=_FakeTuner(-0.10))
        r_tuned = tuned._check_ev("EURUSD", "LONG", 100.0, 99.0, 101.0, 0.45, 0.2)
        assert r_tuned.passed is True


# ── Zone conviction softened for counter-trend under the EV gate ─────────────


def _make_fvg(kind="BEARISH") -> FairValueGap:
    return FairValueGap(
        kind=kind, top=1.0850, bottom=1.0840, midpoint=1.0845,
        size_pips=10.0, strength="STRONG", status=FVGStatus.OPEN,
        candle_index=50, timestamp=_ts(), timeframe="M5",
    )


def _make_ob(kind="BEARISH") -> OrderBlock:
    return OrderBlock(
        kind=kind, top=1.0855, bottom=1.0835, midpoint=1.0845,
        origin_index=30, strength="STRONG", status=OBStatus.FRESH,
        impulse_size=25.0, timestamp=_ts(), timeframe="H1", breaker=False,
    )


def _make_structure(trend=Trend.BULLISH) -> StructureAnalysis:
    return StructureAnalysis(
        trend=trend, last_event=StructureEvent.BOS_BULLISH,
        swing_high=1.0900, swing_low=1.0800,
        last_bos_level=1.0870, last_choch_level=None,
        structure_broken=False,
        bullish_swing_points=[], bearish_swing_points=[],
        confidence=0.8,
    )


def _counter_trend_overlap_model():
    """Bearish FVG+OB overlap under BULLISH HTF bias → counter-trend SHORT."""
    store = WorldModelStore()
    return build_world_model(
        symbol="EURUSD",
        version=store.next_version(),
        fvgs={"M5": [_make_fvg(kind="BEARISH")]},
        order_blocks={"H1": [_make_ob(kind="BEARISH")]},
        structure={"H4": _make_structure(Trend.BULLISH)},
    )


class TestCounterTrendConviction:
    def test_conviction_softened_for_counter_trend(self):
        # EV gate ON → counter-trend FVG+OB = 100 × 0.85 = 85 (reaches the gate).
        model = _counter_trend_overlap_model()
        zones = extract_entry_zones(model, EntryConfig(ev_gate_enabled=True))
        overlap = [z for z in zones if z.zone_type == ZoneType.FVG_OB_OVERLAP]
        assert len(overlap) == 1
        assert overlap[0].is_counter_trend is True
        assert overlap[0].conviction == 85

    def test_conviction_legacy_for_alignment(self):
        # EV gate OFF → legacy haircut 0.70 → 100 × 0.70 = 70 (below score gate).
        model = _counter_trend_overlap_model()
        zones = extract_entry_zones(model, EntryConfig(ev_gate_enabled=False))
        overlap = [z for z in zones if z.zone_type == ZoneType.FVG_OB_OVERLAP]
        assert len(overlap) == 1
        assert overlap[0].conviction == 70


# ── Full gate integration ────────────────────────────────────────────────────


class TestFullGateWithEv:
    def test_full_gate_with_ev(self):
        gate = EntryGate()
        passed, results = gate.validate_all(**_defaults())
        assert passed is True
        assert len(results) == 11
        ev = next(r for r in results if r.gate_name == "ev_gate")
        assert ev.passed is True

    def test_probabilities_from_bias_dict(self):
        # The orchestrator reads long/short probability from the WorldModel bias
        # dict (Phase 3). Verify the keys are surfaced via bias_dict().
        store = WorldModelStore()
        wm = build_world_model(
            symbol="EURUSD",
            version=store.next_version(),
            bias={
                "direction": "LONG", "score": 70,
                "long_probability": 0.65, "short_probability": 0.15,
            },
        )
        bias = wm.bias_dict()
        assert bias.get("long_probability") == 0.65
        assert bias.get("short_probability") == 0.15
