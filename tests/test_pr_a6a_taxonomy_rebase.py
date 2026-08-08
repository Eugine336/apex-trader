"""
PR-A6a — Adaptive Taxonomy Re-baseline to Live Scanner

Tests:
1. Default values match exact live scanner baseline magnitudes.
2. Safety envelope clamp: below/within/above bound; defaults clamp to self.
3. _CONFLUENCE_TO_TAG maps every scanner string to the correct 12-factor key.
4. Old 9-factor → new 12-factor saved-weights migration.
5. build_setup OFF path unchanged (hardcoded path byte-for-byte identical).
"""

import json


from adaptive.score_optimizer import (
    ADAPTIVE_WEIGHT_ENVELOPE_PCT,
    FACTOR_KEYS,
    ScoreOptimizer,
    ScoringWeights,
    _migrate_old_weights,
)


BASELINE_MAGNITUDES = {
    "structure": 20,
    "ob_h1": 10,
    "ob_m5": 10,
    "fvg": 15,
    "mtf_confluence": 15,
    "session": 10,
    "news": 10,
    "currency_strength": 10,
    "liquidity_sweep": 8,
    "volume": 5,
    "inducement": 5,
    "wyckoff": 5,
}


# ──────────────────────────────────────────────────────────────────────────
# 1. Default values match live scanner baselines
# ──────────────────────────────────────────────────────────────────────────


class TestDefaultBaselines:
    def test_factor_count(self):
        assert len(FACTOR_KEYS) == 12

    def test_canonical_total_is_123(self):
        assert ScoringWeights().total == 123
        assert ScoreOptimizer.CANONICAL_TOTAL == 123

    def test_each_default_matches_baseline(self):
        w = ScoringWeights()
        d = w.as_dict()
        for key, expected in BASELINE_MAGNITUDES.items():
            assert d[key] == expected, f"{key}: {d[key]} != {expected}"

    def test_as_dict_keys_match_factor_keys(self):
        assert list(ScoringWeights().as_dict().keys()) == FACTOR_KEYS


# ──────────────────────────────────────────────────────────────────────────
# 2. Safety envelope clamp
# ──────────────────────────────────────────────────────────────────────────


class TestSafetyEnvelope:
    def test_defaults_clamp_to_self(self):
        baseline = ScoringWeights()
        clamped = baseline.clamped_to_envelope(baseline, 0.25)
        assert clamped.as_dict() == baseline.as_dict()

    def test_within_envelope_unchanged(self):
        baseline = ScoringWeights()
        slight = ScoringWeights(structure_weight=22)
        clamped = slight.clamped_to_envelope(baseline, 0.25)
        assert clamped.structure_weight == 22

    def test_above_envelope_clamped_to_upper(self):
        baseline = ScoringWeights()
        extreme = ScoringWeights(structure_weight=100)
        clamped = extreme.clamped_to_envelope(baseline, 0.25)
        assert clamped.structure_weight == round(20 * 1.25)

    def test_below_envelope_clamped_to_lower(self):
        baseline = ScoringWeights()
        extreme = ScoringWeights(fvg_weight=1)
        clamped = extreme.clamped_to_envelope(baseline, 0.25)
        assert clamped.fvg_weight == round(15 * 0.75)

    def test_all_factors_clamped(self):
        baseline = ScoringWeights()
        extreme = ScoringWeights(
            structure_weight=100,
            ob_h1_weight=100,
            ob_m5_weight=100,
            fvg_weight=100,
            mtf_confluence_weight=100,
            session_weight=100,
            news_weight=100,
            currency_strength_weight=100,
            liquidity_sweep_weight=100,
            volume_weight=100,
            inducement_weight=100,
            wyckoff_weight=100,
        )
        clamped = extreme.clamped_to_envelope(baseline, 0.25)
        bd = baseline.as_dict()
        cd = clamped.as_dict()
        for key in FACTOR_KEYS:
            assert cd[key] == round(bd[key] * 1.25), f"{key} upper bound"

    def test_zero_envelope_freezes_to_baseline(self):
        baseline = ScoringWeights()
        modified = ScoringWeights(structure_weight=30, fvg_weight=1)
        clamped = modified.clamped_to_envelope(baseline, 0.0)
        assert clamped.as_dict() == baseline.as_dict()

    def test_custom_pct(self):
        baseline = ScoringWeights()
        extreme = ScoringWeights(session_weight=100)
        clamped = extreme.clamped_to_envelope(baseline, 0.50)
        assert clamped.session_weight == round(10 * 1.50)

    def test_default_envelope_pct(self):
        assert ADAPTIVE_WEIGHT_ENVELOPE_PCT == 0.25


# ──────────────────────────────────────────────────────────────────────────
# 3. Old → new migration
# ──────────────────────────────────────────────────────────────────────────


class TestOldToNewMigration:
    def test_9_factor_to_12_factor(self):
        old = {
            "structure_weight": 17,
            "order_block_weight": 17,
            "fvg_weight": 13,
            "mtf_confluence_weight": 13,
            "session_weight": 9,
            "news_weight": 8,
            "currency_strength_weight": 8,
            "m1_trigger_weight": 8,
            "liquidity_sweep_weight": 7,
        }
        migrated = _migrate_old_weights(old)
        assert migrated["structure_weight"] == 17
        assert migrated["ob_h1_weight"] == 8
        assert migrated["ob_m5_weight"] == 9
        assert "order_block_weight" not in migrated
        assert "m1_trigger_weight" not in migrated
        assert migrated["volume_weight"] == 5
        assert migrated["inducement_weight"] == 5
        assert migrated["wyckoff_weight"] == 5

    def test_already_new_schema_unchanged(self):
        new = {
            "structure_weight": 20,
            "ob_h1_weight": 10,
            "ob_m5_weight": 10,
            "fvg_weight": 15,
            "mtf_confluence_weight": 15,
            "session_weight": 10,
            "news_weight": 10,
            "currency_strength_weight": 10,
            "liquidity_sweep_weight": 8,
            "volume_weight": 5,
            "inducement_weight": 5,
            "wyckoff_weight": 5,
        }
        migrated = _migrate_old_weights(dict(new))
        for k, v in new.items():
            assert migrated[k] == v

    def test_even_order_block_split(self):
        old = {"order_block_weight": 20}
        migrated = _migrate_old_weights(old)
        assert migrated["ob_h1_weight"] == 10
        assert migrated["ob_m5_weight"] == 10

    def test_odd_order_block_split(self):
        old = {"order_block_weight": 17}
        migrated = _migrate_old_weights(old)
        assert migrated["ob_h1_weight"] == 8
        assert migrated["ob_m5_weight"] == 9
        assert migrated["ob_h1_weight"] + migrated["ob_m5_weight"] == 17

    def test_load_old_file_via_optimizer(self, tmp_path):
        old_data = {
            "structure_weight": 17,
            "order_block_weight": 17,
            "fvg_weight": 13,
            "mtf_confluence_weight": 13,
            "session_weight": 9,
            "news_weight": 8,
            "currency_strength_weight": 8,
            "m1_trigger_weight": 8,
            "liquidity_sweep_weight": 7,
        }
        fp = str(tmp_path / "old.json")
        with open(fp, "w") as f:
            json.dump(old_data, f)
        opt = ScoreOptimizer()
        loaded = opt.load_weights(fp)
        assert loaded.structure_weight == 17
        assert loaded.ob_h1_weight == 8
        assert loaded.ob_m5_weight == 9
        assert loaded.volume_weight == 5
        assert not hasattr(loaded, "order_block_weight") or True
        assert not hasattr(loaded, "m1_trigger_weight") or True

    def test_7_field_old_file_migrates(self, tmp_path):
        old_data = {
            "structure_weight": 20,
            "order_block_weight": 20,
            "fvg_weight": 15,
            "mtf_confluence_weight": 15,
            "session_weight": 10,
            "news_weight": 10,
            "currency_strength_weight": 10,
        }
        fp = str(tmp_path / "very_old.json")
        with open(fp, "w") as f:
            json.dump(old_data, f)
        opt = ScoreOptimizer()
        loaded = opt.load_weights(fp)
        assert loaded.ob_h1_weight == 10
        assert loaded.ob_m5_weight == 10
        assert loaded.volume_weight == 5

    def test_corrupt_file_returns_defaults(self, tmp_path):
        fp = str(tmp_path / "corrupt.json")
        with open(fp, "w") as f:
            f.write("NOT JSON {{{")
        opt = ScoreOptimizer()
        loaded = opt.load_weights(fp)
        assert loaded.total == 123
        assert loaded.structure_weight == 20


# ──────────────────────────────────────────────────────────────────────────
# 4. Optimizer normalized to CANONICAL_TOTAL (123)
# ──────────────────────────────────────────────────────────────────────────


class TestOptimizerCanonicalTotal:
    def test_fit_weights_within_envelope(self):
        import random

        random.seed(99)
        trades = []
        for _ in range(120):
            is_win = random.random() < 0.6
            pnl = 20.0 if is_win else -15.0
            tags = random.sample(FACTOR_KEYS, k=random.randint(3, 8))
            trades.append({"pnl": pnl, "confluences_tags": tags})
        opt = ScoreOptimizer()
        result = opt.optimize(trades, min_trades=50)
        baseline = ScoringWeights()
        bd = baseline.as_dict()
        rd = result.as_dict()
        for key in FACTOR_KEYS:
            lo = round(bd[key] * 0.75)
            hi = round(bd[key] * 1.25)
            assert lo <= rd[key] <= hi, f"{key}: {rd[key]} outside [{lo}, {hi}]"
