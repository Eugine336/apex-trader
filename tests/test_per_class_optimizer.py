"""Tests for the per-class ScoreOptimizer (Learning Phase 5).

A single global confluence-weight set averages forex + synthetic + crypto into
a mediocre middle. ``per_class_optimizer`` keeps a separate weight profile per
asset class plus a shared ``default`` used for cold-start, unknown symbols, and
as the Bayesian-shrinkage prior for thin classes. These tests cover:

* asset-class classification (reuses the central instrument registry)
* legacy single-profile behaviour stays byte-for-byte unchanged
* per-class split + independent optimisation
* thin-class fallback to the global profile
* Bayesian shrinkage toward the global prior
* nested persistence round-trip + single-profile reader compatibility
* scanner per-symbol weight resolution
* the ScoreOptimizerTunable per-class snapshot/restore
* config defaults
"""

import json

import pytest

from config import ScoringConfig
from adaptive.score_optimizer import (
    DEFAULT_CLASS,
    FACTOR_KEYS,
    ScoreOptimizer,
    ScoringWeights,
    classify_asset_class,
    load_saved_weights,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _trade(pair: str, tags: list[str], pnl: float, day: int = 1) -> dict:
    return {
        "pair": pair,
        "confluences_tags": list(tags),
        "pnl": pnl,
        "timestamp": f"2026-01-{(day % 27) + 1:02d}T00:00:00",
    }


def _class_trades(pair: str, winning_tag: str, n: int) -> list[dict]:
    """Build n trades for one pair where ``winning_tag`` predicts winners."""
    out = []
    for i in range(n):
        present = i % 2 == 0
        tags = ["structure", winning_tag] if present else ["structure"]
        pnl = 1.0 if present else -1.0
        out.append(_trade(pair, tags, pnl, day=i))
    return out


@pytest.fixture
def tmp_path_opt(tmp_path, monkeypatch):
    """Point the optimizer at a temp weights file."""
    path = str(tmp_path / "scoring_weights.json")
    monkeypatch.setattr(ScoreOptimizer, "DEFAULT_PATH", path)
    return path


# ── Classification ───────────────────────────────────────────────────────────


class TestClassifyAssetClass:
    def test_forex(self):
        assert classify_asset_class("EURUSD") == "forex"
        assert classify_asset_class("gbpjpy") == "forex"

    def test_synthetic(self):
        assert classify_asset_class("BOOM1000") == "synthetic"
        assert classify_asset_class("V75_1S") == "synthetic"

    def test_crypto(self):
        assert classify_asset_class("BTCUSD") == "crypto"
        assert classify_asset_class("ETHUSD") == "crypto"

    def test_commodity_and_index(self):
        assert classify_asset_class("XAUUSD") == "commodity"
        assert classify_asset_class("US100") == "index"

    def test_unknown_and_empty_default(self):
        assert classify_asset_class("ZZZ_NOPE") == DEFAULT_CLASS
        assert classify_asset_class("") == DEFAULT_CLASS


# ── Config defaults ──────────────────────────────────────────────────────────


class TestConfigDefaults:
    def test_per_class_defaults_off(self):
        cfg = ScoringConfig()
        assert cfg.per_class_optimizer is True
        assert cfg.min_trades_per_class == 30
        assert cfg.class_shrinkage_strength == pytest.approx(0.3)

    def test_optimizer_reads_config(self):
        cfg = ScoringConfig(
            per_class_optimizer=True,
            min_trades_per_class=12,
            class_shrinkage_strength=0.5,
        )
        opt = ScoreOptimizer(config=cfg)
        assert opt.per_class is True
        assert opt.min_trades_per_class == 12
        assert opt.class_shrinkage_strength == pytest.approx(0.5)

    def test_optimizer_no_config_is_legacy(self):
        opt = ScoreOptimizer()
        assert opt.per_class is False
        assert opt.class_weights == {}


# ── Legacy behaviour unchanged ───────────────────────────────────────────────


class TestLegacyUnchanged:
    def test_single_profile_optimize_no_class_weights(self, tmp_path_opt):
        opt = ScoreOptimizer()  # per_class False
        trades = _class_trades("EURUSD", "fvg", 60)
        result = opt.optimize(trades, min_trades=20)
        assert isinstance(result, ScoringWeights)
        # No per-class profiles created in legacy mode.
        assert opt.class_weights == {}

    def test_weights_for_symbol_returns_global_in_legacy(self, tmp_path_opt):
        opt = ScoreOptimizer()
        assert opt.weights_for_symbol("EURUSD") is opt.current_weights
        assert opt.weights_for_class("anything") is opt.current_weights

    def test_flat_file_loads_without_class_weights(self, tmp_path, monkeypatch):
        path = str(tmp_path / "flat.json")
        json.dump(ScoringWeights(fvg_weight=18).__dict__, open(path, "w"))
        monkeypatch.setattr(ScoreOptimizer, "DEFAULT_PATH", path)
        opt = ScoreOptimizer()
        assert opt.current_weights.fvg_weight == 18
        assert opt.class_weights == {}


# ── Per-class optimisation ───────────────────────────────────────────────────


class TestPerClassOptimize:
    def test_independent_profiles_per_class(self, tmp_path_opt):
        cfg = ScoringConfig(per_class_optimizer=True, min_trades_per_class=20)
        opt = ScoreOptimizer(config=cfg)
        # forex: fvg predicts winners. synthetic: liquidity_sweep predicts winners.
        trades = _class_trades("EURUSD", "fvg", 60) + _class_trades(
            "BOOM1000", "liquidity_sweep", 60
        )
        opt.optimize(trades, min_trades=20)
        assert set(opt.class_weights.keys()) == {"forex", "synthetic"}
        # Each class lifts a different factor relative to the global profile.
        assert (
            opt.weights_for_symbol("EURUSD").fvg_weight
            >= opt.current_weights.fvg_weight
        )
        assert (
            opt.weights_for_symbol("BOOM1000").liquidity_sweep_weight
            >= opt.current_weights.liquidity_sweep_weight
        )

    def test_thin_class_falls_back_to_global(self, tmp_path_opt):
        cfg = ScoringConfig(per_class_optimizer=True, min_trades_per_class=40)
        opt = ScoreOptimizer(config=cfg)
        # forex has plenty; synthetic has too few to diverge.
        trades = _class_trades("EURUSD", "fvg", 60) + _class_trades(
            "BOOM1000", "liquidity_sweep", 10
        )
        opt.optimize(trades, min_trades=20)
        assert "synthetic" not in opt.class_weights
        # Synthetic symbol resolves to the global profile.
        assert opt.weights_for_symbol("BOOM1000") is opt.current_weights

    def test_unknown_symbol_uses_global(self, tmp_path_opt):
        cfg = ScoringConfig(per_class_optimizer=True, min_trades_per_class=20)
        opt = ScoreOptimizer(config=cfg)
        trades = _class_trades("EURUSD", "fvg", 60)
        opt.optimize(trades, min_trades=20)
        # An unknown symbol classifies to default -> global profile.
        assert opt.weights_for_symbol("ZZZ_NOPE") is opt.current_weights

    def test_global_profile_fitted_on_all_trades(self, tmp_path_opt):
        cfg = ScoringConfig(per_class_optimizer=True, min_trades_per_class=20)
        opt = ScoreOptimizer(config=cfg)
        trades = _class_trades("EURUSD", "fvg", 40) + _class_trades(
            "BOOM1000", "liquidity_sweep", 40
        )
        result = opt.optimize(trades, min_trades=20)
        # optimize returns the global profile; it stays a valid ScoringWeights.
        assert result is opt.current_weights
        assert all(getattr(result, f"{k}_weight") >= ScoreOptimizer.MIN_WEIGHT for k in FACTOR_KEYS)

    def test_class_weights_stay_within_envelope(self, tmp_path_opt):
        cfg = ScoringConfig(per_class_optimizer=True, min_trades_per_class=20)
        opt = ScoreOptimizer(config=cfg)
        trades = _class_trades("EURUSD", "fvg", 80)
        opt.optimize(trades, min_trades=20)
        base = ScoringWeights().as_dict()
        forex = opt.weights_for_class("forex").as_dict()
        for k in FACTOR_KEYS:
            lo = round(base[k] * 0.75)
            hi = round(base[k] * 1.25)
            assert lo <= forex[k] <= hi, f"{k}={forex[k]} outside envelope [{lo},{hi}]"


# ── Bayesian shrinkage ───────────────────────────────────────────────────────


class TestShrinkage:
    def test_thin_class_pulled_more_toward_global(self, tmp_path_opt):
        cfg = ScoringConfig(per_class_optimizer=True, min_trades_per_class=20)
        opt = ScoreOptimizer(config=cfg)
        fitted = ScoringWeights(fvg_weight=19)
        glob = ScoringWeights(fvg_weight=15)
        thin = opt._shrink_toward_global(fitted, glob, n=20)
        thick = opt._shrink_toward_global(fitted, glob, n=400)
        # Thin sample blends closer to the global prior (15); thick trusts fit (19).
        assert abs(thin.fvg_weight - glob.fvg_weight) <= abs(
            thick.fvg_weight - glob.fvg_weight
        )
        assert thick.fvg_weight >= thin.fvg_weight

    def test_zero_strength_no_shrinkage(self, tmp_path_opt):
        cfg = ScoringConfig(
            per_class_optimizer=True,
            min_trades_per_class=20,
            class_shrinkage_strength=0.0,
        )
        opt = ScoreOptimizer(config=cfg)
        fitted = ScoringWeights(fvg_weight=18)
        glob = ScoringWeights(fvg_weight=15)
        # lam = n/(n+0) = 1 -> the class fit is used directly.
        blended = opt._shrink_toward_global(fitted, glob, n=25)
        assert blended.fvg_weight == 18


# ── Persistence ──────────────────────────────────────────────────────────────


class TestPersistence:
    def test_nested_round_trip(self, tmp_path_opt):
        cfg = ScoringConfig(per_class_optimizer=True, min_trades_per_class=20)
        opt = ScoreOptimizer(config=cfg)
        trades = _class_trades("EURUSD", "fvg", 60) + _class_trades(
            "BOOM1000", "liquidity_sweep", 60
        )
        opt.optimize(trades, min_trades=20)

        data = json.load(open(tmp_path_opt))
        assert DEFAULT_CLASS in data
        assert "forex" in data and "synthetic" in data

        reloaded = ScoreOptimizer(config=cfg)
        assert set(reloaded.class_weights.keys()) == {"forex", "synthetic"}
        assert (
            reloaded.weights_for_class("forex").fvg_weight
            == opt.weights_for_class("forex").fvg_weight
        )

    def test_load_saved_weights_extracts_default_from_nested(self, tmp_path):
        path = str(tmp_path / "nested.json")
        json.dump(
            {
                DEFAULT_CLASS: ScoringWeights(fvg_weight=14).__dict__,
                "forex": ScoringWeights(fvg_weight=19).__dict__,
            },
            open(path, "w"),
        )
        # Single-profile readers transparently get the shared default profile.
        assert load_saved_weights(path).fvg_weight == 14

    def test_flat_file_with_per_class_optimizer_loads_global(self, tmp_path, monkeypatch):
        path = str(tmp_path / "flat.json")
        json.dump(ScoringWeights(fvg_weight=17).__dict__, open(path, "w"))
        monkeypatch.setattr(ScoreOptimizer, "DEFAULT_PATH", path)
        cfg = ScoringConfig(per_class_optimizer=True)
        opt = ScoreOptimizer(config=cfg)
        assert opt.current_weights.fvg_weight == 17
        assert opt.class_weights == {}


# ── Scanner per-symbol resolution ────────────────────────────────────────────


class TestScannerResolution:
    def _resolver(self, payload):
        from scanner.pair_scanner import PairScanner

        class _Stub:
            pass

        stub = _Stub()
        stub._adaptive_weights = payload
        return lambda pair: PairScanner._weights_for(stub, pair)

    def test_flat_payload_returns_same_for_all(self):
        resolve = self._resolver({"fvg": 18, "structure": 20})
        assert resolve("EURUSD") == {"fvg": 18, "structure": 20}
        assert resolve("BOOM1000") == {"fvg": 18, "structure": 20}

    def test_nested_payload_resolves_per_class(self):
        resolve = self._resolver(
            {
                "default": {"fvg": 14},
                "forex": {"fvg": 19},
                "synthetic": {"fvg": 11},
            }
        )
        assert resolve("EURUSD")["fvg"] == 19
        assert resolve("BOOM1000")["fvg"] == 11
        # crypto has no profile -> default
        assert resolve("BTCUSD")["fvg"] == 14

    def test_none_payload_returns_none(self):
        resolve = self._resolver(None)
        assert resolve("EURUSD") is None


# ── Tunable adapter ──────────────────────────────────────────────────────────


class TestScoreOptimizerTunablePerClass:
    def test_read_params_includes_classes_when_per_class(self, tmp_path_opt):
        from adaptive.tunable_adapters import ScoreOptimizerTunable

        cfg = ScoringConfig(per_class_optimizer=True, min_trades_per_class=20)
        opt = ScoreOptimizer(config=cfg)
        opt.optimize(_class_trades("EURUSD", "fvg", 60), min_trades=20)
        t = ScoreOptimizerTunable(opt, lambda: [])
        params = t.get_current_params()
        assert "classes" in params
        assert "forex" in params["classes"]

    def test_read_params_flat_when_legacy(self, tmp_path_opt):
        from adaptive.tunable_adapters import ScoreOptimizerTunable

        opt = ScoreOptimizer()  # legacy
        t = ScoreOptimizerTunable(opt, lambda: [])
        params = t.get_current_params()
        assert "classes" not in params

    def test_apply_params_restores_classes(self, tmp_path_opt):
        from adaptive.tunable_adapters import ScoreOptimizerTunable

        cfg = ScoringConfig(per_class_optimizer=True, min_trades_per_class=20)
        opt = ScoreOptimizer(config=cfg)
        opt.optimize(_class_trades("EURUSD", "fvg", 60), min_trades=20)
        t = ScoreOptimizerTunable(opt, lambda: [])
        snapshot = t.get_current_params()

        # Mutate, then restore from snapshot.
        opt.class_weights = {}
        opt.current_weights = ScoringWeights(fvg_weight=3)
        t._apply_params(snapshot)
        assert "forex" in opt.class_weights
        assert opt.current_weights.fvg_weight == snapshot["fvg"]

    def test_validate_params_ignores_classes_key(self, tmp_path_opt):
        from adaptive.tunable_adapters import ScoreOptimizerTunable

        cfg = ScoringConfig(per_class_optimizer=True, min_trades_per_class=20)
        opt = ScoreOptimizer(config=cfg)
        t = ScoreOptimizerTunable(opt, lambda: [])
        params = dict(ScoringWeights().as_dict())
        params["classes"] = {"forex": ScoringWeights().as_dict()}
        ok, _ = t.validate_params(params)
        assert ok is True


# ── Grand flip: continuous_pair_multiplier ───────────────────────────────────


class TestContinuousPairMultiplierFlip:
    def test_pair_learner_config_default_on(self):
        from config import PairLearnerConfig

        assert PairLearnerConfig().continuous_pair_multiplier is True
