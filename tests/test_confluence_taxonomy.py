"""
APEX TRADER — Confluence Taxonomy Tests
Verifies the canonical 9-factor tag mapping matches real scanner/entry strings,
the order_block fix (H1 OB / M5 OB), and the 9-key ScoringWeights schema.
"""

import random

from adaptive.score_optimizer import FACTOR_KEYS, ScoreOptimizer, ScoringWeights
from platforms.main_loop import _parse_confluence_tags

CANONICAL_9 = [
    "structure",
    "order_block",
    "fvg",
    "mtf_confluence",
    "session",
    "news",
    "currency_strength",
    "m1_trigger",
    "liquidity_sweep",
]


# ──────────────────────────────────────────────────────────────────────────
# Tag mapping — real scanner strings
# ──────────────────────────────────────────────────────────────────────────


class TestConfluenceTagMapping:
    def test_structure_aligned(self):
        tags = _parse_confluence_tags(["Structure aligned (STRONG)"])
        assert tags == ["structure"]

    def test_h1_ob_maps_to_order_block(self):
        tags = _parse_confluence_tags(["H1 OB bias (STRONG bearish)"])
        assert tags == ["order_block"]

    def test_m5_ob_maps_to_order_block(self):
        tags = _parse_confluence_tags(["M5 OB entry zone (1.0850–1.0855)"])
        assert tags == ["order_block"]

    def test_legacy_order_block_prefix(self):
        tags = _parse_confluence_tags(["Order block confirmed at zone"])
        assert tags == ["order_block"]

    def test_fvg_entry_zone(self):
        tags = _parse_confluence_tags(["FVG entry zone (1.0840–1.0845)"])
        assert tags == ["fvg"]

    def test_mtf_confluence(self):
        tags = _parse_confluence_tags(["Multi-TF FVG confluence"])
        assert tags == ["mtf_confluence"]

    def test_session_active(self):
        tags = _parse_confluence_tags(["Session active (LONDON)"])
        assert tags == ["session"]

    def test_news_clear(self):
        tags = _parse_confluence_tags(["News clear"])
        assert tags == ["news"]

    def test_news_filter_na(self):
        tags = _parse_confluence_tags(["News filter N/A (synthetic)"])
        assert tags == ["news"]

    def test_currency_strength(self):
        tags = _parse_confluence_tags(["Currency strength (EUR strongest, USD weakest)"])
        assert tags == ["currency_strength"]

    def test_m1_confirmed(self):
        tags = _parse_confluence_tags(["M1 confirmed: pin_bar — Bearish pin bar"])
        assert tags == ["m1_trigger"]

    def test_m1_generic(self):
        tags = _parse_confluence_tags(["M1 pattern detected at zone"])
        assert tags == ["m1_trigger"]

    def test_liquidity_sweep_detected(self):
        tags = _parse_confluence_tags(["Liquidity sweep detected (+8)"])
        assert tags == ["liquidity_sweep"]

    def test_liquidity_sweep_confirmed(self):
        tags = _parse_confluence_tags(["Liquidity sweep confirmed at entry zone"])
        assert tags == ["liquidity_sweep"]

    def test_liquidity_generic(self):
        tags = _parse_confluence_tags(["Liquidity event near zone"])
        assert tags == ["liquidity_sweep"]

    def test_multiple_confluences_maps_all(self):
        raw = [
            "Structure aligned (STRONG)",
            "H1 OB bias (STRONG bearish)",
            "FVG entry zone (1.0840–1.0845)",
            "Session active (LONDON)",
            "News clear",
            "Currency strength (EUR strongest)",
            "M1 confirmed: engulfing",
            "Liquidity sweep detected (+8)",
            "Multi-TF FVG confluence",
        ]
        tags = _parse_confluence_tags(raw)
        assert set(tags) == set(CANONICAL_9)

    def test_dedup_h1_and_m5_ob(self):
        raw = ["H1 OB bias (STRONG)", "M5 OB entry zone (1.08)"]
        tags = _parse_confluence_tags(raw)
        assert tags == ["order_block"]

    def test_unrecognized_string_skipped(self):
        tags = _parse_confluence_tags(["Something totally unknown"])
        assert tags == []

    def test_non_string_input_handled(self):
        tags = _parse_confluence_tags([42, None, True, "FVG entry zone"])
        assert tags == ["fvg"]

    def test_empty_list(self):
        assert _parse_confluence_tags([]) == []


# ──────────────────────────────────────────────────────────────────────────
# ScoringWeights — 9-factor schema
# ──────────────────────────────────────────────────────────────────────────


class TestScoringWeights9Factor:
    def test_factor_keys_count(self):
        assert len(FACTOR_KEYS) == 9

    def test_factor_keys_canonical(self):
        assert FACTOR_KEYS == CANONICAL_9

    def test_default_weights_sum_to_100(self):
        w = ScoringWeights()
        assert w.total == 100

    def test_as_dict_has_9_keys(self):
        d = ScoringWeights().as_dict()
        assert len(d) == 9
        assert set(d.keys()) == set(CANONICAL_9)

    def test_all_defaults_above_min_weight(self):
        d = ScoringWeights().as_dict()
        assert all(v >= ScoreOptimizer.MIN_WEIGHT for v in d.values())

    def test_new_fields_exist(self):
        w = ScoringWeights()
        assert hasattr(w, "m1_trigger_weight")
        assert hasattr(w, "liquidity_sweep_weight")
        assert w.m1_trigger_weight > 0
        assert w.liquidity_sweep_weight > 0

    def test_optimize_returns_9_key_weights(self):
        random.seed(42)
        all_tags = list(CANONICAL_9)
        trades = []
        for _ in range(120):
            is_win = random.random() < 0.7
            pnl = round(random.uniform(5, 40), 2) if is_win else round(random.uniform(-30, -3), 2)
            tags = random.sample(all_tags, k=random.randint(3, 7))
            trades.append(
                {
                    "pnl": pnl,
                    "confluences_tags": tags,
                }
            )
        opt = ScoreOptimizer()
        result = opt.optimize(trades, min_trades=50)
        assert result.total == 100
        d = result.as_dict()
        assert len(d) == 9
        assert all(v >= ScoreOptimizer.MIN_WEIGHT for v in d.values())

    def test_factor_effectiveness_covers_all_9(self):
        all_tags = list(CANONICAL_9)
        trades = [{"pnl": 10.0, "confluences_tags": all_tags}] * 20
        eff = ScoreOptimizer().get_factor_effectiveness(trades)
        assert set(eff.keys()) == set(CANONICAL_9)

    def test_backward_compat_load_7_field_file(self, tmp_path):
        import json

        old_data = {
            "structure_weight": 20,
            "order_block_weight": 20,
            "fvg_weight": 15,
            "mtf_confluence_weight": 15,
            "session_weight": 10,
            "news_weight": 10,
            "currency_strength_weight": 10,
        }
        fp = str(tmp_path / "old_weights.json")
        with open(fp, "w") as f:
            json.dump(old_data, f)
        opt = ScoreOptimizer()
        loaded = opt.load_weights(fp)
        assert loaded.structure_weight == 20
        assert loaded.m1_trigger_weight == 8
        assert loaded.liquidity_sweep_weight == 7
