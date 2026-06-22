"""APEX TRADER — live in-trade thesis validation tests.

Covers the management-loop un-freezing work:
  * The fast M5 structure feed contributes to ``tf_alignment`` and
    ``structure_integrity`` for an open trade (previously pinned to the slow
    H1/H4/D1 reads between hourly closes).
  * The live directional consensus panel reaches ``assess_open_trade`` and
    drives bounded CLOSE pressure when it opposes the open position.
  * The fast-opposition decay weight default is high enough to actually
    contribute exit pressure (was 0.15, capped below the HOLD floor).
"""

from types import SimpleNamespace

from decision.context import TradeContext
from decision.engine import DecisionEngine
from decision.actions import Action
from decision.situation import SituationEngine, SituationAssessment


def _vote(module, direction, confidence=0.8, weight=1.0):
    return SimpleNamespace(
        module=module, direction=direction, confidence=confidence, weight=weight,
    )


class TestM5FastFeed:
    def setup_method(self):
        self.se = SituationEngine()

    def test_m5_trend_contributes_to_alignment(self):
        # HTF all UNKNOWN — only M5 carries a read. Without the M5 feed the
        # alignment would be a flat 0.0 (the frozen-loop symptom).
        ctx = TradeContext(
            symbol="EURGBP", direction="SELL",
            m5_trend="BEARISH", m5_confidence=0.9,
        )
        sa = self.se.assess_open_trade(ctx)
        assert sa.tf_alignment > 0.0          # M5 supports the short
        assert "M5" in sa.tf_components
        assert sa.tf_components["M5"] > 0.0

    def test_m5_break_lowers_structure_integrity(self):
        ctx = TradeContext(
            symbol="EURGBP", direction="SELL",
            m5_event="BOS_BULLISH",            # opposes a short
        )
        sa = self.se.assess_open_trade(ctx)
        assert sa.structure_integrity < 0.5
        assert sa.structure_components.get("M5", 0.0) < 0.0


class TestConsensusReachesManagement:
    def setup_method(self):
        self.se = SituationEngine()
        self.de = DecisionEngine()

    def test_open_trade_consensus_alignment_populated(self):
        # A panel opposing the open short → negative consensus alignment.
        votes = [
            _vote("order_block", "LONG"),
            _vote("fvg", "LONG"),
            _vote("momentum", "LONG"),
        ]
        ctx = TradeContext(
            symbol="EURGBP", direction="SELL", consensus_votes=votes,
        )
        sa = self.se.assess_open_trade(ctx)
        assert sa.consensus_alignment < 0.0
        assert "order_block" in sa.consensus_components.get("against", [])

    def test_no_votes_is_inert(self):
        ctx = TradeContext(symbol="EURGBP", direction="SELL", consensus_votes=[])
        sa = self.se.assess_open_trade(ctx)
        assert sa.consensus_alignment == 0.0

    def test_opposing_consensus_adds_close_pressure(self):
        # A near-flat, mildly-adverse losing short: on its own it HOLDs. A
        # strongly opposing live panel must tip it into a CLOSE.
        ctx = TradeContext(
            symbol="EURGBP", direction="SELL",
            original_risk_pips=10.0, pnl_pips=-2.0,
        )
        sa_oppose = SituationAssessment(
            tf_alignment=0.0, structure_integrity=0.45,
            momentum=-0.4, profit_state=-0.2,
            consensus_alignment=-1.0,
            consensus_components={"against": ["order_block", "fvg", "liquidity"]},
        )
        d_oppose = self.de.decide_management(ctx, sa_oppose)
        assert d_oppose.action == Action.CLOSE
        assert "consensus opposes" in d_oppose.reason

        sa_neutral = SituationAssessment(
            tf_alignment=0.0, structure_integrity=0.45,
            momentum=-0.4, profit_state=-0.2,
            consensus_alignment=0.0, consensus_components={},
        )
        d_neutral = self.de.decide_management(ctx, sa_neutral)
        # Without the opposing panel the same context holds.
        assert d_neutral.action != Action.CLOSE

    def test_consensus_aware_disabled_is_inert(self):
        ctx = TradeContext(
            symbol="EURGBP", direction="SELL",
            original_risk_pips=10.0, pnl_pips=-2.0,
        )
        sa_oppose = SituationAssessment(
            tf_alignment=0.0, structure_integrity=0.45,
            momentum=-0.4, profit_state=-0.2,
            consensus_alignment=-1.0,
            consensus_components={"against": ["order_block", "fvg"]},
        )
        de_off = DecisionEngine(consensus_aware=False)
        d = de_off.decide_management(ctx, sa_oppose)
        assert "consensus opposes" not in d.reason


class TestFastOppositionDefault:
    def test_default_weight_raised(self):
        assert DecisionEngine().fast_opposition_decay_weight == 0.30
