"""
APEX TRADER — Session 3: learned decision thresholds.

Covers the opportunistic-trading "no hardcoded intelligence" requirement that
the decision-layer thresholds are LEARNED from the system's own track record,
with conservative cold-start priors, bounded movement and staleness decay:

* GateTuner learns the HTF-alignment gate floor (new ``htf_alignment`` family)
  the same shadow-fed way it already learns the entry-score bar.
* The learned offset reaches the LIVE EntryGate._check_alignment, bounded by an
  absolute permissive cap and never tightening past the operator's floor.
* GateTuner staleness decay returns a learned offset toward neutral when its
  shadow evidence dries up.
* Live entry-gate quality-gate rejections are fed to the shadow store so the
  tuner actually has live counterfactual data (previously it never did).
* The threshold-hierarchy registry stays internally consistent.
* The already-wired learned components default ON.
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from adaptive.gate_tuner import GateTuner
from entry.entry_gate import EntryGate
from entry.models import EntryConfig, EntryZone, ZoneType

# ── helpers ──────────────────────────────────────────────────────────────────


def _gt(**kw) -> GateTuner:
    tmp = tempfile.mkdtemp()
    return GateTuner(os.path.join(tmp, "g.json"), **kw)


def _rows(gate: str, win: int, loss: int):
    out = []
    if win:
        out.append({"rejecting_gate": f"{gate}:x", "outcome": "WIN", "cnt": win, "avg_r": 1.0})
    if loss:
        out.append({"rejecting_gate": f"{gate}:x", "outcome": "LOSS", "cnt": loss, "avg_r": -1.0})
    return out


def _make_zone() -> EntryZone:
    now = datetime.now(timezone.utc)
    return EntryZone(
        symbol="EURUSD",
        direction="LONG",
        zone_type=ZoneType.FVG_MIDPOINT,
        top=1.0850,
        bottom=1.0840,
        midpoint=1.0845,
        invalidation_level=1.0835,
        conviction=85,
        created_at=now,
        expires_at=now + timedelta(seconds=600),
        timeframe="M5",
    )


def _defaults(**overrides):
    base = dict(
        symbol="EURUSD",
        direction="LONG",
        entry_price=1.0845,
        stop_loss=1.0835,
        tp1=1.0860,
        tp2=1.0875,
        score=85,
        current_spread_pips=1.0,
        zone=_make_zone(),
        is_instrument_known=True,
        is_market_open=True,
        is_session_active=True,
        is_news_clear=True,
        is_drawdown_ok=True,
    )
    base.update(overrides)
    return base


# ── GateTuner: htf_alignment family ──────────────────────────────────────────


class TestHtfAlignmentTunable:
    def test_whitelisted(self):
        assert "htf_alignment" in GateTuner.TUNABLE
        assert "htf_alignment" in _gt().all_offsets()

    def test_cold_start_neutral(self):
        assert _gt().offset("htf_alignment") == 0.0

    def test_loosen_when_rejected_setups_win(self):
        gt = _gt()
        # 28 WIN / 12 LOSS = 70% over 40 resolved → loosen one step (-0.05).
        gt.calibrate(_rows("htf_alignment", 28, 12))
        assert gt.offset("htf_alignment") == -0.05

    def test_tighten_back_toward_neutral(self):
        gt = _gt()
        gt.calibrate(_rows("htf_alignment", 100, 0))
        assert gt.offset("htf_alignment") == -0.05
        gt.calibrate(_rows("htf_alignment", 0, 100))
        assert gt.offset("htf_alignment") == 0.0

    def test_envelope_clamp(self):
        gt = _gt()
        for _ in range(40):
            gt.calibrate(_rows("htf_alignment", 100, 0))
        assert gt.offset("htf_alignment") == -0.5  # clamped at envelope floor

    def test_min_samples_gate(self):
        gt = _gt()
        gt.calibrate(_rows("htf_alignment", 20, 0))  # < MIN_SAMPLES
        assert gt.offset("htf_alignment") == 0.0


# ── GateTuner: staleness decay ───────────────────────────────────────────────


class TestStalenessDecay:
    def test_absent_family_decays_toward_neutral(self):
        gt = _gt()
        gt.calibrate(_rows("htf_alignment", 100, 0))
        assert gt.offset("htf_alignment") == -0.05
        # A pass with evidence for a DIFFERENT gate only: htf_alignment is now
        # stale → decays one step back toward neutral.
        gt.calibrate(_rows("entry_engine", 100, 0))
        assert gt.offset("htf_alignment") == 0.0

    def test_present_family_not_decayed(self):
        gt = _gt()
        gt.calibrate(_rows("htf_alignment", 100, 0))
        # htf_alignment keeps producing winning rejections → loosens further,
        # never decays while it is present in the evidence.
        gt.calibrate(_rows("htf_alignment", 100, 0))
        assert gt.offset("htf_alignment") == -0.10

    def test_thin_present_family_not_decayed(self):
        gt = _gt()
        gt.calibrate(_rows("htf_alignment", 100, 0))
        assert gt.offset("htf_alignment") == -0.05
        # Present but below MIN_SAMPLES → still accumulating, not stale.
        gt.calibrate(_rows("htf_alignment", 10, 0))
        assert gt.offset("htf_alignment") == -0.05

    def test_decay_disabled(self):
        gt = _gt(staleness_decay=False)
        gt.calibrate(_rows("htf_alignment", 100, 0))
        assert gt.offset("htf_alignment") == -0.05
        gt.calibrate(_rows("entry_engine", 100, 0))
        # No decay: the stale offset is retained.
        assert gt.offset("htf_alignment") == -0.05

    def test_decay_never_overshoots_neutral(self):
        gt = _gt()
        gt.calibrate(_rows("htf_alignment", 100, 0))  # -0.05
        gt.calibrate(_rows("entry_engine", 100, 0))  # decay → 0.0
        gt.calibrate(_rows("entry_engine", 100, 0))  # already neutral, no-op
        assert gt.offset("htf_alignment") == 0.0


# ── EntryGate: learned HTF-alignment offset reaches the live gate ─────────────


class _FakeTuner:
    def __init__(self, htf_offset: float = 0.0):
        self._htf = htf_offset

    def offset(self, family: str) -> float:
        return self._htf if family == "htf_alignment" else 0.0


class TestEntryGateHtfAlignmentLearned:
    def test_offset_lowers_floor_admits_counter_htf(self):
        cfg = EntryConfig(min_htf_alignment=-0.5, ev_gate_enabled=False)
        # Base floor -0.5 rejects alignment -0.6; a -0.2 loosening offset lowers
        # the floor to -0.7, so the same setup now passes.
        base = EntryGate(config=cfg)
        passed_base, _ = base.validate_all(**_defaults(alignment=-0.6))
        assert passed_base is False

        tuned = EntryGate(config=cfg, gate_tuner=_FakeTuner(-0.2))
        passed_tuned, results = tuned.validate_all(**_defaults(alignment=-0.6))
        align = next(r for r in results if r.gate_name == "alignment")
        assert align.passed is True

    def test_offset_never_below_permissive_floor(self):
        cfg = EntryConfig(
            min_htf_alignment=-0.5, min_htf_alignment_floor=-0.8,
            ev_gate_enabled=False,
        )
        # An out-of-envelope huge loosening can't drop the floor below -0.8, so a
        # fully-opposed setup (-0.95) is still rejected.
        gate = EntryGate(config=cfg, gate_tuner=_FakeTuner(-50.0))
        passed, results = gate.validate_all(**_defaults(alignment=-0.95))
        align = next(r for r in results if r.gate_name == "alignment")
        assert align.passed is False

    def test_cold_start_offset_zero_is_base_behaviour(self):
        cfg = EntryConfig(min_htf_alignment=-0.5, ev_gate_enabled=False)
        tuned = EntryGate(config=cfg, gate_tuner=_FakeTuner(0.0))
        # offset 0 → identical to the no-tuner gate.
        assert tuned.validate_all(**_defaults(alignment=-0.6))[0] is False
        assert tuned.validate_all(**_defaults(alignment=-0.3))[0] is True

    def test_no_tuner_uses_base_floor(self):
        cfg = EntryConfig(min_htf_alignment=-0.5, ev_gate_enabled=False)
        gate = EntryGate(config=cfg)  # no tuner
        passed, _ = gate.validate_all(**_defaults(alignment=-0.6))
        assert passed is False


# ── Live entry-gate rejections feed the shadow store (GateTuner data) ─────────


class _GateResult:
    def __init__(self, name, passed, reason="r"):
        self.gate_name = name
        self.passed = passed
        self.reason = reason


class _RecordingSystem:
    """Minimal stand-in exercising the real recording helper from the system."""

    from event_driven_bootstrap import EventDrivenSystem as _EDS

    _TUNABLE_GATE_FAMILY = _EDS._TUNABLE_GATE_FAMILY
    _record_entry_gate_shadow_rejections = _EDS._record_entry_gate_shadow_rejections

    def __init__(self):
        self.recorded = []

    def _record_shadow_rejection(self, symbol, direction, entry, sl, tp, gate, conviction=0.0):
        self.recorded.append(gate)


_META = {"entry_price": 1.0845, "stop_loss": 1.0835, "tp1": 1.0860, "conviction": 90.0}


class TestEntryGateShadowRecording:
    def test_records_quality_gate_families(self):
        sysm = _RecordingSystem()
        results = [
            _GateResult("score_minimum", False),
            _GateResult("alignment", False),
        ]
        sysm._record_entry_gate_shadow_rejections("EURUSD", "LONG", results, _META)
        families = [g.split(":", 1)[0] for g in sysm.recorded]
        assert "entry_engine" in families
        assert "htf_alignment" in families
        assert len(families) == 2

    def test_skips_safety_gates(self):
        sysm = _RecordingSystem()
        results = [
            _GateResult("market_open", False),
            _GateResult("spread_ok", False),
            _GateResult("zone_valid", False),
            _GateResult("risk_reward_ok", False),
        ]
        sysm._record_entry_gate_shadow_rejections("EURUSD", "LONG", results, _META)
        assert sysm.recorded == []

    def test_skips_passing_gates(self):
        sysm = _RecordingSystem()
        results = [_GateResult("score_minimum", True), _GateResult("alignment", False)]
        sysm._record_entry_gate_shadow_rejections("EURUSD", "LONG", results, _META)
        assert [g.split(":", 1)[0] for g in sysm.recorded] == ["htf_alignment"]

    def test_skips_when_prices_invalid(self):
        sysm = _RecordingSystem()
        results = [_GateResult("alignment", False)]
        sysm._record_entry_gate_shadow_rejections(
            "EURUSD",
            "LONG",
            results,
            {"entry_price": 0.0, "stop_loss": 0.0, "tp1": 0.0},
        )
        assert sysm.recorded == []


# ── Threshold-hierarchy registry stays consistent ────────────────────────────


class TestThresholdRegistry:
    def test_tiers_disjoint_and_classified(self):
        from adaptive import threshold_registry as tr

        keys_by_tier = {t: [e.key for e in es] for t, es in tr.by_tier().items()}
        all_keys = [k for ks in keys_by_tier.values() for k in ks]
        assert len(all_keys) == len(set(all_keys)), "a threshold appears in two tiers"

    def test_headline_classifications(self):
        from adaptive import threshold_registry as tr

        # Guardrails are never learned.
        assert tr.tier_of("min_structural_rr") == tr.TIER_GUARDRAIL
        assert tr.tier_of("min_htf_alignment_floor") == tr.TIER_GUARDRAIL
        # The learned thresholds this session touches.
        assert tr.tier_of("htf_alignment_floor") == tr.TIER_LEARNED
        assert tr.tier_of("module_vote_weights") == tr.TIER_LEARNED
        assert tr.tier_of("opportunity_win_rate") == tr.TIER_LEARNED
        # Structure-derived, per Sessions 1–2.
        assert tr.tier_of("tp_targets") == tr.TIER_STRUCTURE

    def test_unknown_key(self):
        from adaptive import threshold_registry as tr

        assert tr.tier_of("does_not_exist") == ""


# ── Already-wired learned components default ON ──────────────────────────────


class TestLearnedComponentsEnabledByDefault:
    def test_vote_calibration_on(self):
        from config import VoteCalibratorConfig

        assert VoteCalibratorConfig().vote_calibration_enabled is True

    def test_adaptive_win_rate_on(self):
        from config import OpportunityRankerConfig

        assert OpportunityRankerConfig().adaptive_win_rate_provider_enabled is True
