"""
Session 4 — Signal Fidelity tests (collapse points #11, #16, #25, #26, #28).

Each fix preserves richer per-module / per-situation evidence that the old
(direction, confidence) / scalar collapses discarded. Tests cover BOTH the legacy
path (flag off) and the enriched path (flag on) and assert backward compatibility:
every vote extractor still unpacks as a 2-tuple.

Dependency-light — the momentum tests monkeypatch the RSI/MACD calculators so no
pandas data construction is needed.
"""

import pytest
from types import SimpleNamespace

from brain.directional_consensus import (
    Vote,
    VoteResult,
    vote_from_structure,
    vote_from_order_blocks,
    vote_from_fvg,
    vote_from_momentum,
)
from decision.context import EntryContext, TradeContext
from decision.situation import SituationEngine, SituationAssessment


# ── #26 — VoteResult carries evidence but stays a (direction, confidence) tuple ─

class TestVoteResultBackwardCompatible:
    def test_unpacks_as_two_tuple(self):
        r = VoteResult("LONG", 0.8, {"foo": 1})
        d, c = r                      # legacy unpack still works
        assert d == "LONG"
        assert c == 0.8
        assert r[0] == "LONG" and r[1] == 0.8
        assert len(r) == 2

    def test_evidence_accessible_and_copied(self):
        ev = {"rsi": 75}
        r = VoteResult("SHORT", 0.5, ev)
        assert r.evidence == {"rsi": 75}
        assert r.direction == "SHORT" and r.confidence == 0.5
        # returned dict is a copy — mutating it doesn't corrupt the result
        r.evidence["rsi"] = 0
        assert r.evidence == {"rsi": 75}

    def test_empty_evidence_default(self):
        r = VoteResult("NEUTRAL", 0.0)
        assert r.evidence == {}

    def test_extractor_returns_voteresult_with_evidence(self):
        r = vote_from_structure({"direction": "BULLISH", "confidence": 0.9})
        d, c = r
        assert d == "LONG"
        assert r.evidence["raw_direction"] == "BULLISH"


class TestVoteCarriesEvidence:
    def test_vote_accepts_evidence_field(self):
        v = Vote("momentum", "LONG", 0.8, 1.0, evidence={"rsi": 80})
        assert v.evidence == {"rsi": 80}

    def test_vote_evidence_defaults_empty(self):
        v = Vote("momentum", "LONG", 0.8, 1.0)
        assert v.evidence == {}

    def test_evidence_excluded_from_equality(self):
        # Two votes that differ only in evidence are still equal (evidence is
        # additive context, not part of the vote's identity).
        a = Vote("momentum", "LONG", 0.8, 1.0, evidence={"rsi": 80})
        b = Vote("momentum", "LONG", 0.8, 1.0, evidence={"rsi": 10})
        assert a == b

    def test_signed_unaffected_by_evidence(self):
        v = Vote("momentum", "LONG", 0.5, 2.0, evidence={"x": 1})
        assert v.signed == pytest.approx(1.0)


# ── #11 — Momentum continuous confidence ──────────────────────────────────────

def _patch_momentum(monkeypatch, rsi, macd):
    import brain.momentum_divergence as md
    monkeypatch.setattr(md, "calculate_rsi", lambda *a, **k: rsi)
    monkeypatch.setattr(md, "calculate_macd", lambda *a, **k: macd)


_DF = {"close": [1.0, 2.0, 3.0]}   # never read — RSI/MACD are patched


class TestMomentumContinuousConfidence:
    def test_legacy_path_both_agree_is_constant_08(self, monkeypatch):
        # RSI overbought (SHORT) + MACD below signal (SHORT) → agree.
        _patch_momentum(monkeypatch, rsi=90.0, macd=(-2.0, -1.0))
        d, c = vote_from_momentum(_DF, _DF, continuous_confidence=False)
        assert d == "SHORT"
        assert c == pytest.approx(0.8)

    def test_legacy_path_single_source_is_constant_04(self, monkeypatch):
        # RSI neutral (50), MACD only → one source.
        _patch_momentum(monkeypatch, rsi=50.0, macd=(2.0, 1.0))
        d, c = vote_from_momentum(_DF, _DF, continuous_confidence=False)
        assert d == "LONG"
        assert c == pytest.approx(0.4)

    def test_continuous_both_agree_in_band(self, monkeypatch):
        _patch_momentum(monkeypatch, rsi=90.0, macd=(-2.0, -1.0))
        d, c = vote_from_momentum(_DF, _DF, continuous_confidence=True)
        assert d == "SHORT"
        assert 0.6 <= c <= 1.0          # both-agree band
        assert c != pytest.approx(0.8)  # no longer pinned to the constant

    def test_continuous_gradient_strong_beats_marginal(self, monkeypatch):
        # Pinned overbought (RSI 99) should out-confidence a barely-overbought 71
        # when MACD strength is held equal.
        _patch_momentum(monkeypatch, rsi=99.0, macd=(-2.0, -1.0))
        _, c_strong = vote_from_momentum(_DF, _DF, continuous_confidence=True)
        _patch_momentum(monkeypatch, rsi=71.0, macd=(-2.0, -1.0))
        _, c_weak = vote_from_momentum(_DF, _DF, continuous_confidence=True)
        assert c_strong > c_weak

    def test_continuous_single_source_in_lower_band(self, monkeypatch):
        _patch_momentum(monkeypatch, rsi=50.0, macd=(2.0, 1.0))
        d, c = vote_from_momentum(_DF, _DF, continuous_confidence=True)
        assert d == "LONG"
        assert 0.3 <= c <= 0.6

    def test_disagree_is_neutral(self, monkeypatch):
        # RSI overbought (SHORT) but MACD above signal (LONG) → conflict.
        _patch_momentum(monkeypatch, rsi=80.0, macd=(2.0, 1.0))
        d, c = vote_from_momentum(_DF, _DF, continuous_confidence=True)
        assert d == "NEUTRAL"
        assert c == 0.0

    def test_evidence_carries_rsi_and_macd(self, monkeypatch):
        _patch_momentum(monkeypatch, rsi=90.0, macd=(-2.0, -1.0))
        r = vote_from_momentum(_DF, _DF)
        assert r.evidence["rsi"] == pytest.approx(90.0)
        assert r.evidence["rsi_dir"] == "SHORT"
        assert r.evidence["macd_dir"] == "SHORT"
        assert r.evidence["agree"] is True


# ── #25 — OB/FVG zone confluence (stacking) ───────────────────────────────────

def _ob(kind, strength, status, top=None, bottom=None):
    return SimpleNamespace(kind=kind, strength=strength, status=status,
                           top=top, bottom=bottom)


class TestZoneConfluence:
    def test_legacy_max_ignores_stacking(self):
        from brain.order_block import OBStatus
        price = 1.0820
        one = [_ob("BULLISH", "WEAK", OBStatus.FRESH, top=1.0800)]
        three = [
            _ob("BULLISH", "WEAK", OBStatus.FRESH, top=1.0800),
            _ob("BULLISH", "WEAK", OBStatus.FRESH, top=1.0790),
            _ob("BULLISH", "WEAK", OBStatus.FRESH, top=1.0780),
        ]
        _, c1 = vote_from_order_blocks(one, price, confluence_bonus=False)
        _, c3 = vote_from_order_blocks(three, price, confluence_bonus=False)
        assert c1 == pytest.approx(c3)   # max() — stacking invisible

    def test_confluence_rewards_stacking(self):
        from brain.order_block import OBStatus
        price = 1.0820
        one = [_ob("BULLISH", "WEAK", OBStatus.FRESH, top=1.0800)]
        three = [
            _ob("BULLISH", "WEAK", OBStatus.FRESH, top=1.0800),
            _ob("BULLISH", "WEAK", OBStatus.FRESH, top=1.0790),
            _ob("BULLISH", "WEAK", OBStatus.FRESH, top=1.0780),
        ]
        d1, c1 = vote_from_order_blocks(one, price, confluence_bonus=True)
        d3, c3 = vote_from_order_blocks(three, price, confluence_bonus=True)
        assert d1 == d3 == "LONG"
        assert c3 > c1                   # three stacked OBs read stronger

    def test_confluence_clamped_to_one(self):
        from brain.order_block import OBStatus
        price = 1.0820
        many = [_ob("BULLISH", "STRONG", OBStatus.FRESH, top=1.0800 - i * 0.0001)
                for i in range(6)]
        _, c = vote_from_order_blocks(many, price, confluence_bonus=True)
        assert c <= 1.0

    def test_evidence_reports_zone_counts(self):
        from brain.order_block import OBStatus
        price = 1.0820
        obs = [
            _ob("BULLISH", "MODERATE", OBStatus.FRESH, top=1.0800),
            _ob("BULLISH", "WEAK", OBStatus.TESTED, top=1.0790),
        ]
        r = vote_from_order_blocks(obs, price)
        assert r.evidence["bull_zone_count"] == 2
        assert r.evidence["bear_zone_count"] == 0

    def test_fvg_confluence_rewards_stacking(self):
        from brain.fvg_detector import FVGStatus
        price = 1.0820
        one = [SimpleNamespace(kind="BULLISH", strength="WEAK",
                               status=FVGStatus.OPEN, top=1.0800, bottom=1.0790)]
        three = [
            SimpleNamespace(kind="BULLISH", strength="WEAK",
                            status=FVGStatus.OPEN, top=1.0800, bottom=1.0790),
            SimpleNamespace(kind="BULLISH", strength="WEAK",
                            status=FVGStatus.OPEN, top=1.0795, bottom=1.0785),
            SimpleNamespace(kind="BULLISH", strength="WEAK",
                            status=FVGStatus.OPEN, top=1.0790, bottom=1.0780),
        ]
        _, c1 = vote_from_fvg(one, price, 0.005, confluence_bonus=True)
        _, c3 = vote_from_fvg(three, price, 0.005, confluence_bonus=True)
        assert c3 > c1

    def test_direction_winner_unchanged_by_confluence(self):
        # A strong bear stack must still beat a single weak bull.
        from brain.order_block import OBStatus
        price = 1.0820
        obs = [
            _ob("BULLISH", "WEAK", OBStatus.TESTED, top=1.0800),
            _ob("BEARISH", "STRONG", OBStatus.FRESH, bottom=1.0850),
            _ob("BEARISH", "STRONG", OBStatus.FRESH, bottom=1.0860),
        ]
        d, _ = vote_from_order_blocks(obs, price, confluence_bonus=True)
        assert d == "SHORT"


# ── #16 — Situation momentum / structure component breakdown ──────────────────

class TestSituationComponents:
    def test_entry_momentum_components_populated(self):
        eng = SituationEngine()
        ctx = EntryContext(symbol="EURUSD", direction="LONG",
                            m1_aligned_count=5, m1_event="BOS_BULLISH",
                            entry_type="OB_MIDPOINT")
        sa = eng.assess_entry(ctx)
        comps = sa.momentum_vector()
        assert "candle" in comps and "event" in comps
        assert comps["event"] > 0          # supporting BOS adds positive event term

    def test_entry_structure_components_identify_timeframe(self):
        eng = SituationEngine()
        # H4 breaks against a LONG — the component map must show WHICH TF broke.
        ctx = EntryContext(symbol="EURUSD", direction="LONG",
                            h4_event="BOS_BEARISH", entry_type="OB_MIDPOINT")
        sa = eng.assess_entry(ctx)
        comps = sa.structure_vector()
        assert comps["H4"] < 0             # H4 is the breaker
        assert comps["D1"] == 0.0          # D1 intact

    def test_open_trade_momentum_components(self):
        eng = SituationEngine()
        ctx = TradeContext(symbol="EURUSD", direction="BUY",
                           m1_aligned_count=5, m1_event="BOS_BULLISH",
                           score_history=[60, 70, 85])
        sa = eng.assess_open_trade(ctx)
        comps = sa.momentum_vector()
        assert comps["candle"] > 0
        assert comps["trajectory"] > 0     # improving score history

    def test_open_trade_structure_components(self):
        eng = SituationEngine()
        ctx = TradeContext(symbol="EURUSD", direction="SELL",
                           h1_event="BOS_BULLISH")   # opposes a short
        sa = eng.assess_open_trade(ctx)
        comps = sa.structure_vector()
        assert comps["H1"] < 0

    def test_scalar_momentum_unchanged_by_component_capture(self):
        # The scalar value must equal the sum of its component contributions
        # (capturing provenance does not alter the read).
        eng = SituationEngine()
        ctx = EntryContext(symbol="EURUSD", direction="LONG",
                           m1_aligned_count=4, m1_event="NONE",
                           entry_type="OB_MIDPOINT")
        sa = eng.assess_entry(ctx)
        comps = sa.momentum_vector()
        recomputed = comps["candle"] + comps["event"] + comps["trajectory"]
        assert sa.momentum == pytest.approx(max(-1.0, min(1.0, recomputed)), abs=1e-3)


# ── #28 — Urgency / confidence provenance ─────────────────────────────────────

class TestUrgencyAndConfidenceProvenance:
    def test_urgency_keeps_both_sources(self):
        eng = SituationEngine()
        # News imminent AND session not tradeable — max() hides the loser; the
        # component map must keep both.
        ctx = TradeContext(symbol="EURUSD", direction="BUY",
                           minutes_to_high_impact_news=3.0, news_impact="HIGH",
                           session_tradeable=False)
        sa = eng.assess_open_trade(ctx)
        comps = sa.urgency_vector()
        assert comps["news"] > 0
        assert comps["session"] == pytest.approx(0.6)
        # scalar is still the max of the two
        assert sa.urgency == pytest.approx(max(comps["news"], comps["session"]))

    def test_confidence_distinguishes_presence_from_quality(self):
        eng = SituationEngine()
        # Trends are present (presence high) but their confidences are low
        # (quality low) — the two must differ.
        ctx = TradeContext(symbol="EURUSD", direction="BUY",
                           d1_trend="BULLISH", d1_confidence=0.1,
                           h4_trend="BULLISH", h4_confidence=0.1,
                           h1_trend="BULLISH", h1_confidence=0.1,
                           m1_trend="BULLISH",
                           score_history=[50, 55, 60])
        sa = eng.assess_open_trade(ctx)
        comps = sa.confidence_components
        assert comps["presence"] > comps["data_quality"]
        assert comps["data_quality"] == pytest.approx(0.1, abs=1e-6)

    def test_entry_confidence_components_populated(self):
        eng = SituationEngine()
        ctx = EntryContext(symbol="EURUSD", direction="LONG",
                           d1_trend="BULLISH", d1_confidence=0.8,
                           h4_trend="BULLISH", h4_confidence=0.6,
                           h1_trend="BULLISH", h1_confidence=0.7,
                           entry_type="OB_MIDPOINT")
        sa = eng.assess_entry(ctx)
        assert sa.confidence_components["data_quality"] == pytest.approx(0.7, abs=1e-6)
        assert sa.urgency_vector() == {"news": 0.0, "session": 0.0}


class TestSituationAssessmentDefaults:
    def test_new_vectors_default_empty(self):
        sa = SituationAssessment()
        assert sa.momentum_vector() == {}
        assert sa.structure_vector() == {}
        assert sa.urgency_vector() == {}
        assert sa.confidence_components == {}
