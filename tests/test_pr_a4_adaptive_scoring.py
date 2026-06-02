"""
PR-A4 — Adaptive Scoring Weights in MTFOrchestrator

Safety contract enforcement:
1. FLAG-OFF == TODAY, BYTE-FOR-BYTE — identical scores to pre-A4.
2. FLAG-ON at default weights ≈ today.
3. FLAG-ON with non-trivial weights shifts scores predictably.
4. mtf_confluence + currency_strength contribute only when ON.
5. Absent/invalid weights store → canonical defaults, no crash.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from unittest.mock import MagicMock, patch

import pytest

from adaptive.score_optimizer import ScoringWeights
from brain.mtf_orchestrator import Confluence, MTFOrchestrator, TradeSetup


# ---------------------------------------------------------------------------
# Helpers — deterministic mock sub-engines
# ---------------------------------------------------------------------------


class _FakeEvent(Enum):
    CHOCH_BULLISH = "CHOCH_BULLISH"
    CHOCH_BEARISH = "CHOCH_BEARISH"
    BOS_BULLISH = "BOS_BULLISH"
    BOS_BEARISH = "BOS_BEARISH"
    NONE = "NONE"


@dataclass
class _FakeStructureAnalysis:
    last_event: _FakeEvent = _FakeEvent.CHOCH_BULLISH


@dataclass
class _FakeRegimeAnalysis:
    regime: object = None

    def __post_init__(self):
        if self.regime is None:
            self.regime = type("R", (), {"value": "TRENDING", "VOLATILE": None})()


class _FakeMarketRegime:
    VOLATILE = "VOLATILE"
    RANGING = "RANGING"
    TRENDING = "TRENDING"


@dataclass
class _FakeLiqZone:
    price: float


@dataclass
class _FakeLiqMap:
    nearest_sell_liq: object = None
    nearest_buy_liq: object = None
    buy_side_liquidity: list = None
    sell_side_liquidity: list = None

    def __post_init__(self):
        if self.buy_side_liquidity is None:
            self.buy_side_liquidity = []
        if self.sell_side_liquidity is None:
            self.sell_side_liquidity = []


@dataclass
class _FakeNewsResult:
    is_clear: bool = True
    warning_message: str = ""


def _make_df(n: int = 30, close: float = 1.10):
    """Minimal DataFrame that passes the 25-candle gate."""
    import pandas as pd

    data = {
        "open": [close - 0.001] * n,
        "high": [close + 0.001] * n,
        "low": [close - 0.002] * n,
        "close": [close] * n,
        "volume": [100] * n,
        "time": pd.date_range("2026-01-01", periods=n, freq="1min"),
    }
    return pd.DataFrame(data)


def _data_by_tf(close: float = 1.10) -> dict:
    return {tf: _make_df(close=close) for tf in ("H4", "H1", "M15", "M5", "M1")}


def _build_orchestrator(
    use_adaptive: bool = False,
    weights: ScoringWeights = None,
    bias_strength: str = "STRONG",
    bias_direction: str = "BULLISH",
    choch_aligned: bool = True,
    has_fvg: bool = True,
    has_confluence: bool = True,
    has_ob: bool = True,
    sweep: bool = True,
    session_score: int = 8,
    news_clear: bool = True,
    min_entry_score: int = 0,
) -> MTFOrchestrator:
    """Build an orchestrator with fully mocked sub-engines for controlled scoring."""
    from brain.structure_engine import StructureEvent

    orch = MTFOrchestrator(
        min_entry_score=min_entry_score,
        use_adaptive_weights=use_adaptive,
        scoring_weights=weights,
    )

    # Structure engine
    bias_mock = {
        "direction": bias_direction,
        "strength": bias_strength,
        "h4_trend": "BULLISH",
        "h1_trend": "BULLISH",
    }
    orch.structure_engine = MagicMock()
    orch.structure_engine.get_bias.return_value = bias_mock

    if choch_aligned:
        event = StructureEvent.CHOCH_BULLISH if bias_direction == "BULLISH" else StructureEvent.CHOCH_BEARISH
    else:
        event = StructureEvent.NONE

    m1_analysis = MagicMock()
    m1_analysis.last_event = event
    orch.structure_engine.analyze.return_value = m1_analysis

    # FVG detector
    orch.fvg_detector = MagicMock()
    orch.fvg_detector.detect.return_value = []
    fvg_result = {
        "has_confluence": has_confluence,
        "m5_fvg": MagicMock() if has_fvg else None,
        "m15_fvg": MagicMock() if has_confluence else None,
        "overlap_zone": {"midpoint": 1.10, "top": 1.101, "bottom": 1.099} if has_confluence else None,
        "strength": "VERY_STRONG" if has_confluence else ("MODERATE" if has_fvg else "NONE"),
    }
    orch.fvg_detector.get_confluence_fvgs.return_value = fvg_result

    # Order block
    orch.order_block_detector = MagicMock()
    orch.order_block_detector.detect.return_value = []
    ob_entry = MagicMock(midpoint=1.10) if has_ob else None
    orch.order_block_detector.get_entry_ob.return_value = ob_entry

    # Regime
    from brain.regime_detector import MarketRegime, RegimeDetector

    regime_analysis = MagicMock()
    regime_analysis.regime = MarketRegime.TRENDING
    real_regime_det = RegimeDetector()
    orch.regime_detector = MagicMock()
    orch.regime_detector.analyze.return_value = regime_analysis
    orch.regime_detector.adjust_score = real_regime_det.adjust_score

    # Session
    orch.session_engine = MagicMock()
    orch.session_engine.get_session_score.return_value = session_score

    # News
    orch.news_guard = MagicMock()
    orch.news_guard.check.return_value = _FakeNewsResult(is_clear=news_clear)

    # Liquidity
    orch.liquidity_mapper = MagicMock()
    liq_map = MagicMock()
    liq_map.nearest_sell_liq = _FakeLiqZone(1.095) if sweep else None
    liq_map.nearest_buy_liq = _FakeLiqZone(1.105) if sweep else None
    liq_map.buy_side_liquidity = [_FakeLiqZone(1.11)]
    liq_map.sell_side_liquidity = [_FakeLiqZone(1.09)]
    orch.liquidity_mapper.map.return_value = liq_map
    orch.liquidity_mapper.detect_sweep.return_value = sweep

    return orch


# ---------------------------------------------------------------------------
# Contract 1: OFF == TODAY BYTE-FOR-BYTE
# ---------------------------------------------------------------------------


class TestFlagOffIdenticalToHardcoded:
    """When use_adaptive_scoring_weights=False, scores must be identical
    to the original hardcoded implementation for every factor combination."""

    @pytest.mark.parametrize(
        "strength,fvg,confl,ob,choch,sweep,session,expected_raw",
        [
            # All factors maxed
            ("STRONG", True, True, True, True, True, 8, 20 + 10 + 10 + 15 + 20 + 15 + 10),
            # Weaker structure
            ("MODERATE", True, True, True, True, True, 8, 14 + 10 + 10 + 15 + 20 + 15 + 10),
            # No confluence, just M5 FVG
            ("STRONG", True, False, True, True, True, 8, 20 + 10 + 10 + 8 + 20 + 15 + 10),
            # No FVG at all
            ("STRONG", False, False, True, True, True, 8, 20 + 10 + 10 + 0 + 20 + 15 + 10),
            # No OB
            ("STRONG", True, True, False, True, True, 8, 20 + 10 + 10 + 15 + 0 + 15 + 10),
            # No trigger
            ("STRONG", True, True, True, False, True, 8, 20 + 10 + 10 + 15 + 20 + 0 + 10),
            # No sweep
            ("STRONG", True, True, True, True, False, 8, 20 + 10 + 10 + 15 + 20 + 15 + 0),
            # Medium session
            ("STRONG", True, True, True, True, True, 5, 20 + 5 + 10 + 15 + 20 + 15 + 10),
            # Low session
            ("STRONG", True, True, True, True, True, 2, 20 + 0 + 10 + 15 + 20 + 15 + 10),
            # Minimal
            ("MODERATE", False, False, False, False, False, 2, 14 + 0 + 10 + 0 + 0 + 0 + 0),
        ],
        ids=[
            "all_max",
            "weak_structure",
            "fvg_no_confluence",
            "no_fvg",
            "no_ob",
            "no_trigger",
            "no_sweep",
            "medium_session",
            "low_session",
            "minimal",
        ],
    )
    def test_score_matches_hardcoded(
        self, strength, fvg, confl, ob, choch, sweep, session, expected_raw
    ):
        orch = _build_orchestrator(
            use_adaptive=False,
            bias_strength=strength,
            has_fvg=fvg,
            has_confluence=confl,
            has_ob=ob,
            choch_aligned=choch,
            sweep=sweep,
            session_score=session,
        )
        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is not None
        assert setup.score == expected_raw

    def test_no_mtf_confluence_item_when_off(self):
        orch = _build_orchestrator(use_adaptive=False, has_confluence=True)
        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is not None
        names = [c.name for c in setup.confluences]
        assert "Multi-TF FVG" not in names
        assert "Currency Strength" not in names

    def test_confluence_count_is_seven_when_off(self):
        orch = _build_orchestrator(use_adaptive=False)
        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is not None
        assert len(setup.confluences) == 7


# ---------------------------------------------------------------------------
# Contract 2: ON at default weights ≈ today
# ---------------------------------------------------------------------------


class TestFlagOnDefaultWeights:
    """With the flag ON and canonical default weights, scores should be
    close to the hardcoded values (not byte-for-byte identical since the
    factor separation changes the math)."""

    def test_all_max_with_defaults(self):
        orch = _build_orchestrator(use_adaptive=True)
        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is not None
        # All 9 factors at full → sum of all weights = 100
        assert setup.score == 100

    def test_minimal_with_defaults(self):
        orch = _build_orchestrator(
            use_adaptive=True,
            bias_strength="MODERATE",
            has_fvg=False,
            has_confluence=False,
            has_ob=False,
            choch_aligned=False,
            sweep=False,
            session_score=2,
        )
        # structure=round(17*0.7)=12, session=0, news=8, rest=0 → 20
        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is not None
        assert setup.score == 12 + 0 + 8

    def test_nine_confluences_when_on(self):
        orch = _build_orchestrator(use_adaptive=True)
        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is not None
        assert len(setup.confluences) == 9
        names = {c.name for c in setup.confluences}
        assert "Multi-TF FVG" in names
        assert "Currency Strength" in names


# ---------------------------------------------------------------------------
# Contract 3: Non-trivial weights shift scores predictably
# ---------------------------------------------------------------------------


class TestNonTrivialWeightsShiftScores:
    """With custom weights, factor contributions follow their weight."""

    def test_heavy_structure_weight(self):
        heavy = ScoringWeights(
            structure_weight=40,
            order_block_weight=10,
            fvg_weight=10,
            mtf_confluence_weight=5,
            session_weight=10,
            news_weight=5,
            currency_strength_weight=5,
            m1_trigger_weight=10,
            liquidity_sweep_weight=5,
        )
        assert heavy.total == 100

        orch = _build_orchestrator(
            use_adaptive=True,
            weights=heavy,
            bias_strength="STRONG",
        )
        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is not None
        structure_conf = [c for c in setup.confluences if c.name == "Market Structure"][0]
        assert structure_conf.score == 40

    def test_heavy_fvg_weight(self):
        heavy = ScoringWeights(
            structure_weight=5,
            order_block_weight=5,
            fvg_weight=40,
            mtf_confluence_weight=5,
            session_weight=5,
            news_weight=5,
            currency_strength_weight=5,
            m1_trigger_weight=5,
            liquidity_sweep_weight=25,
        )
        assert heavy.total == 100

        orch = _build_orchestrator(
            use_adaptive=True,
            weights=heavy,
            has_fvg=True,
            has_confluence=False,
        )
        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is not None
        fvg_conf = [c for c in setup.confluences if c.name == "FVG Zone"][0]
        assert fvg_conf.score == 40

    def test_score_sum_equals_total_when_all_factors_present(self):
        custom = ScoringWeights(
            structure_weight=25,
            order_block_weight=15,
            fvg_weight=10,
            mtf_confluence_weight=10,
            session_weight=10,
            news_weight=10,
            currency_strength_weight=5,
            m1_trigger_weight=10,
            liquidity_sweep_weight=5,
        )
        assert custom.total == 100

        orch = _build_orchestrator(
            use_adaptive=True,
            weights=custom,
            bias_strength="STRONG",
        )
        setup = orch.build_setup(
            "EURUSD", _data_by_tf(), currency_strength_aligned=True
        )
        assert setup is not None
        assert setup.score == 100

    def test_partial_structure_with_custom_weight(self):
        w = ScoringWeights(structure_weight=30)
        orch = _build_orchestrator(
            use_adaptive=True,
            weights=w,
            bias_strength="MODERATE",
        )
        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is not None
        sc = [c for c in setup.confluences if c.name == "Market Structure"][0]
        assert sc.score == round(30 * 0.7)

    def test_medium_session_with_custom_weight(self):
        w = ScoringWeights(session_weight=20)
        orch = _build_orchestrator(
            use_adaptive=True,
            weights=w,
            session_score=5,
        )
        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is not None
        sess = [c for c in setup.confluences if c.name == "Session Timing"][0]
        assert sess.score == round(20 * 0.5)


# ---------------------------------------------------------------------------
# Contract 4: New buckets only contribute when ON
# ---------------------------------------------------------------------------


class TestNewBucketsGatedByFlag:
    """mtf_confluence and currency_strength must contribute zero when
    the flag is OFF and their weighted value when ON."""

    def test_mtf_confluence_zero_when_off(self):
        orch = _build_orchestrator(use_adaptive=False, has_confluence=True)
        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is not None
        names = [c.name for c in setup.confluences]
        assert "Multi-TF FVG" not in names

    def test_mtf_confluence_scored_when_on(self):
        w = ScoringWeights(mtf_confluence_weight=13)
        orch = _build_orchestrator(use_adaptive=True, weights=w, has_confluence=True)
        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is not None
        mtf = [c for c in setup.confluences if c.name == "Multi-TF FVG"][0]
        assert mtf.score == 13

    def test_mtf_confluence_zero_when_on_but_no_confluence(self):
        orch = _build_orchestrator(
            use_adaptive=True, has_fvg=True, has_confluence=False
        )
        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is not None
        mtf = [c for c in setup.confluences if c.name == "Multi-TF FVG"][0]
        assert mtf.score == 0

    def test_currency_strength_zero_when_off(self):
        orch = _build_orchestrator(use_adaptive=False)
        setup = orch.build_setup(
            "EURUSD", _data_by_tf(), currency_strength_aligned=True
        )
        assert setup is not None
        names = [c.name for c in setup.confluences]
        assert "Currency Strength" not in names

    def test_currency_strength_scored_when_on_and_aligned(self):
        w = ScoringWeights(currency_strength_weight=8)
        orch = _build_orchestrator(use_adaptive=True, weights=w)
        setup = orch.build_setup(
            "EURUSD", _data_by_tf(), currency_strength_aligned=True
        )
        assert setup is not None
        cs = [c for c in setup.confluences if c.name == "Currency Strength"][0]
        assert cs.score == 8

    def test_currency_strength_zero_when_on_but_not_aligned(self):
        orch = _build_orchestrator(use_adaptive=True)
        setup = orch.build_setup(
            "EURUSD", _data_by_tf(), currency_strength_aligned=False
        )
        assert setup is not None
        cs = [c for c in setup.confluences if c.name == "Currency Strength"][0]
        assert cs.score == 0


# ---------------------------------------------------------------------------
# Contract 5: Absent / invalid weights → canonical defaults, no crash
# ---------------------------------------------------------------------------


class TestWeightsStoreFallback:
    """When no saved weights exist or the file is corrupt, the orchestrator
    must fall back to canonical defaults without crashing."""

    def test_no_weights_file_uses_defaults(self):
        with patch("brain.mtf_orchestrator.Path") as mock_path_cls:
            mock_path = MagicMock()
            mock_path.exists.return_value = False
            mock_path_cls.return_value = mock_path
            w = MTFOrchestrator.load_saved_weights()
        assert w.total == 100
        assert w.structure_weight == 17

    def test_corrupt_file_uses_defaults(self):
        import json
        from pathlib import Path

        test_path = Path("data/scoring_weights.json")
        test_path.parent.mkdir(parents=True, exist_ok=True)
        original_exists = test_path.exists()
        original_content = test_path.read_text() if original_exists else None

        try:
            test_path.write_text("NOT JSON {{{")
            w = MTFOrchestrator.load_saved_weights()
            assert w.total == 100
            assert w.structure_weight == 17
        finally:
            if original_content is not None:
                test_path.write_text(original_content)
            elif test_path.exists():
                test_path.unlink()

    def test_valid_file_loads_weights(self):
        import json
        from pathlib import Path

        test_path = Path("data/scoring_weights.json")
        test_path.parent.mkdir(parents=True, exist_ok=True)
        original_exists = test_path.exists()
        original_content = test_path.read_text() if original_exists else None

        custom_data = {
            "structure_weight": 25,
            "order_block_weight": 15,
            "fvg_weight": 10,
            "mtf_confluence_weight": 10,
            "session_weight": 10,
            "news_weight": 10,
            "currency_strength_weight": 5,
            "m1_trigger_weight": 10,
            "liquidity_sweep_weight": 5,
        }

        try:
            test_path.write_text(json.dumps(custom_data))
            w = MTFOrchestrator.load_saved_weights()
            assert w.structure_weight == 25
            assert w.fvg_weight == 10
            assert w.total == 100
        finally:
            if original_content is not None:
                test_path.write_text(original_content)
            elif test_path.exists():
                test_path.unlink()

    def test_none_weights_param_uses_defaults(self):
        orch = MTFOrchestrator(use_adaptive_weights=True, scoring_weights=None)
        assert orch._weights.total == 100
        assert orch._weights.structure_weight == 17

    def test_explicit_weights_param_used(self):
        custom = ScoringWeights(structure_weight=30)
        orch = MTFOrchestrator(use_adaptive_weights=True, scoring_weights=custom)
        assert orch._weights.structure_weight == 30


# ---------------------------------------------------------------------------
# Config flag integration
# ---------------------------------------------------------------------------


class TestConfigFlag:
    """The use_adaptive_scoring_weights flag defaults to False in ScoringConfig."""

    def test_default_is_false(self):
        from config import ScoringConfig
        assert ScoringConfig().use_adaptive_scoring_weights is False

    def test_can_enable(self):
        from config import ScoringConfig
        cfg = ScoringConfig(use_adaptive_scoring_weights=True)
        assert cfg.use_adaptive_scoring_weights is True


# ---------------------------------------------------------------------------
# Behavioral edge cases
# ---------------------------------------------------------------------------


class TestBehavioralEdgeCases:
    """Edge cases: regime caps, min_entry_score gating, news rejection."""

    def test_regime_cap_still_applies(self):
        from brain.regime_detector import MarketRegime

        orch = _build_orchestrator(use_adaptive=True, min_entry_score=0)
        regime_analysis = MagicMock()
        regime_analysis.regime = MarketRegime.RANGING
        orch.regime_detector.analyze.return_value = regime_analysis

        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is not None
        assert setup.score <= 80

    def test_volatile_still_returns_none(self):
        from brain.regime_detector import MarketRegime

        orch = _build_orchestrator(use_adaptive=True)
        regime_analysis = MagicMock()
        regime_analysis.regime = MarketRegime.VOLATILE
        orch.regime_detector.analyze.return_value = regime_analysis

        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is None

    def test_news_block_still_returns_none(self):
        orch = _build_orchestrator(use_adaptive=True, news_clear=False)
        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is None

    def test_min_score_gate_on_adaptive(self):
        orch = _build_orchestrator(
            use_adaptive=True,
            min_entry_score=99,
            bias_strength="MODERATE",
            has_fvg=False,
            has_confluence=False,
            has_ob=False,
            choch_aligned=False,
            sweep=False,
            session_score=2,
        )
        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is None

    def test_currency_strength_param_backward_compat(self):
        """Existing callers that don't pass currency_strength_aligned should
        still work (defaults to False)."""
        orch = _build_orchestrator(use_adaptive=True)
        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is not None
        cs = [c for c in setup.confluences if c.name == "Currency Strength"][0]
        assert cs.score == 0

    def test_fvg_and_mtf_separate_when_on(self):
        """FVG and MTF confluence are independent buckets when adaptive is ON.
        With m5_fvg=True + has_confluence=True, both contribute."""
        w = ScoringWeights(fvg_weight=13, mtf_confluence_weight=13)
        orch = _build_orchestrator(
            use_adaptive=True,
            weights=w,
            has_fvg=True,
            has_confluence=True,
        )
        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is not None
        fvg = [c for c in setup.confluences if c.name == "FVG Zone"][0]
        mtf = [c for c in setup.confluences if c.name == "Multi-TF FVG"][0]
        assert fvg.score == 13
        assert mtf.score == 13

    def test_fvg_only_no_mtf_when_on(self):
        """FVG without multi-TF confluence: only FVG scores."""
        w = ScoringWeights(fvg_weight=13, mtf_confluence_weight=13)
        orch = _build_orchestrator(
            use_adaptive=True,
            weights=w,
            has_fvg=True,
            has_confluence=False,
        )
        setup = orch.build_setup("EURUSD", _data_by_tf())
        assert setup is not None
        fvg = [c for c in setup.confluences if c.name == "FVG Zone"][0]
        mtf = [c for c in setup.confluences if c.name == "Multi-TF FVG"][0]
        assert fvg.score == 13
        assert mtf.score == 0
