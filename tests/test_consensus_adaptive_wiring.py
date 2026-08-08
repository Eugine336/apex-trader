"""Tests for the adaptive vote-panel hooks wired into ``build_consensus``.

Covers Session B Part 1: the VoteCalibrator weight multiplier and the
ModuleGovernor suppression are applied at vote-construction time so every
downstream consumer (``decide``, the opportunity ranker, the situation
assessment) sees the calibrated / filtered panel.

Both hooks must be behaviour-neutral when absent or disabled — that is the
contract that lets the bootstrap wire them in unconditionally.
"""

from __future__ import annotations

from brain.decision_core import build_consensus


class _FakeWM:
    """Minimal duck-typed WorldModel exposing only what build_consensus reads.

    Produces a single bullish structure vote and a single bullish volume vote
    (both at the unbiased default base weight 1.0); everything else returns
    empty so the panel is deterministic and easy to assert on.
    """

    def bias_dict(self) -> dict:
        return {"direction": "LONG", "confidence": 0.8}

    def volume_by_tf(self) -> dict:
        class _Vol:
            confirmation_bias = "BULLISH"
            volume_ratio = 3.0
            has_spike = True

        return {"M5": _Vol()}

    def wyckoff_by_tf(self) -> dict:
        return {}

    def all_order_blocks(self):
        return []

    def all_fvgs(self):
        return []


def _vote_map(votes) -> dict:
    return {v.module: v for v in votes}


class _Calibrator:
    """Scales structure ×2.0 and volume ×0.5; neutral elsewhere."""

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self._mult = {"structure": 2.0, "volume": 0.5}

    def calibrated_weight(self, module: str, base_weight: float) -> float:
        if not self._enabled:
            return base_weight
        return base_weight * self._mult.get(module, 1.0)


class _Governor:
    """Suppresses whatever modules are passed in."""

    def __init__(self, suppressed) -> None:
        self._suppressed = set(suppressed)

    def is_suppressed(self, module: str) -> bool:
        return module in self._suppressed


class TestVoteCalibratorWiring:
    def test_baseline_weights_without_calibrator(self):
        votes, _ = build_consensus("EURUSD", _FakeWM(), 1.10)
        vm = _vote_map(votes)
        # Unbiased default: every module starts on equal footing (1.0).
        assert vm["structure"].weight == 1.0
        assert vm["volume"].weight == 1.0

    def test_calibrated_weights_applied(self):
        votes, _ = build_consensus(
            "EURUSD",
            _FakeWM(),
            1.10,
            vote_calibrator=_Calibrator(),
        )
        vm = _vote_map(votes)
        assert vm["structure"].weight == 2.0  # 1.0 × 2.0
        assert vm["volume"].weight == 0.5  # 1.0 × 0.5

    def test_disabled_calibrator_is_neutral(self):
        votes, _ = build_consensus(
            "EURUSD",
            _FakeWM(),
            1.10,
            vote_calibrator=_Calibrator(enabled=False),
        )
        vm = _vote_map(votes)
        assert vm["structure"].weight == 1.0
        assert vm["volume"].weight == 1.0

    def test_calibrator_exception_falls_back_to_base(self):
        class _Boom:
            def calibrated_weight(self, module, base_weight):
                raise RuntimeError("boom")

        votes, _ = build_consensus(
            "EURUSD",
            _FakeWM(),
            1.10,
            vote_calibrator=_Boom(),
        )
        vm = _vote_map(votes)
        # Failure must not drop or corrupt the vote — base weight preserved.
        assert vm["structure"].weight == 1.0
        assert vm["volume"].weight == 1.0


class TestModuleGovernorWiring:
    def test_suppressed_module_excluded(self):
        votes, _ = build_consensus(
            "EURUSD",
            _FakeWM(),
            1.10,
            module_governor=_Governor({"volume"}),
        )
        vm = _vote_map(votes)
        assert "structure" in vm
        assert "volume" not in vm

    def test_no_suppression_keeps_full_panel(self):
        votes, _ = build_consensus(
            "EURUSD",
            _FakeWM(),
            1.10,
            module_governor=_Governor(set()),
        )
        vm = _vote_map(votes)
        assert "structure" in vm
        assert "volume" in vm

    def test_governor_exception_keeps_vote(self):
        class _Boom:
            def is_suppressed(self, module):
                raise RuntimeError("boom")

        votes, _ = build_consensus(
            "EURUSD",
            _FakeWM(),
            1.10,
            module_governor=_Boom(),
        )
        vm = _vote_map(votes)
        # A failing governor must never silently drop the panel.
        assert "structure" in vm
        assert "volume" in vm


class TestCombinedHooks:
    def test_suppress_then_calibrate(self):
        votes, _ = build_consensus(
            "EURUSD",
            _FakeWM(),
            1.10,
            vote_calibrator=_Calibrator(),
            module_governor=_Governor({"volume"}),
        )
        vm = _vote_map(votes)
        # volume suppressed entirely; structure still calibrated ×2.0
        assert "volume" not in vm
        assert vm["structure"].weight == 2.0


class TestConsensusWeightWiring:
    """The per-module base weights come from ConsensusConfig.weights, so the
    operator can retune the panel without a code edit and no module carries a
    hardcoded advantage by default."""

    def test_config_weights_override_defaults(self):
        votes, _ = build_consensus(
            "EURUSD",
            _FakeWM(),
            1.10,
            weights={"structure": 3.0, "volume": 0.25},
        )
        vm = _vote_map(votes)
        assert vm["structure"].weight == 3.0
        assert vm["volume"].weight == 0.25

    def test_partial_weights_fall_back_to_unit_default(self):
        # Only structure overridden; volume falls back to the 1.0 default.
        votes, _ = build_consensus(
            "EURUSD",
            _FakeWM(),
            1.10,
            weights={"structure": 5.0},
        )
        vm = _vote_map(votes)
        assert vm["structure"].weight == 5.0
        assert vm["volume"].weight == 1.0

    def test_calibrator_composes_on_top_of_config_weight(self):
        votes, _ = build_consensus(
            "EURUSD",
            _FakeWM(),
            1.10,
            weights={"structure": 3.0},
            vote_calibrator=_Calibrator(),  # structure ×2.0
        )
        vm = _vote_map(votes)
        assert vm["structure"].weight == 6.0  # 3.0 (config) × 2.0 (calibrator)

    def test_default_panel_is_equal_weighted(self):
        votes, _ = build_consensus("EURUSD", _FakeWM(), 1.10)
        assert votes
        assert all(v.weight == 1.0 for v in votes)


class _WinRateProvider:
    """Resolves every pair to a fixed calibrated win probability."""

    def __init__(self, rate: float) -> None:
        self._rate = rate

    def for_pair(self, pair: str, regime: str = ""):
        rate = self._rate

        def _cb(direction: str, timeframe_class: str):
            return rate

        return _cb, None


class TestWinRateProviderWiring:
    def test_candidate_ev_uses_calibrated_win_rate(self):
        _, cands = build_consensus(
            "EURUSD",
            _FakeWM(),
            1.10,
            win_rate_provider=_WinRateProvider(0.75),
        )
        assert cands, "expected at least one ranked opportunity"
        for c in cands:
            assert abs(c.win_prob - 0.75) < 1e-9
            assert c.win_prob_components.get("calibrated") is True

    def test_no_provider_uses_modelled_win_rate(self):
        _, cands = build_consensus("EURUSD", _FakeWM(), 1.10)
        assert cands
        for c in cands:
            # No provider → modelled prior path, never flagged calibrated.
            assert c.win_prob_components.get("calibrated") is False

    def test_provider_exception_falls_back_gracefully(self):
        class _Boom:
            def for_pair(self, pair, regime=""):
                raise RuntimeError("boom")

        # A failing provider must not break candidate production.
        _, cands = build_consensus(
            "EURUSD",
            _FakeWM(),
            1.10,
            win_rate_provider=_Boom(),
        )
        assert cands
        for c in cands:
            assert c.win_prob_components.get("calibrated") is False
