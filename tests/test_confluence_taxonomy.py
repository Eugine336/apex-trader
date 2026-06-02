"""
APEX TRADER — Confluence Taxonomy Tests
Verifies the canonical 12-factor tag mapping matches real scanner/entry strings,
the split OB keys (ob_h1 / ob_m5), new factors (volume, inducement, wyckoff),
and the 12-key ScoringWeights schema matching live scanner baselines.
"""

import random

from adaptive.score_optimizer import FACTOR_KEYS, ScoreOptimizer, ScoringWeights
from platforms.main_loop import _parse_confluence_tags

CANONICAL_12 = [
    "structure",
    "ob_h1",
    "ob_m5",
    "fvg",
    "mtf_confluence",
    "session",
    "news",
    "currency_strength",
    "liquidity_sweep",
    "volume",
    "inducement",
    "wyckoff",
]


# ──────────────────────────────────────────────────────────────────────────
# Tag mapping — real scanner strings
# ──────────────────────────────────────────────────────────────────────────


class TestConfluenceTagMapping:
    def test_structure_aligned(self):
        tags = _parse_confluence_tags(["Structure aligned (STRONG)"])
        assert tags == ["structure"]

    def test_h1_ob_maps_to_ob_h1(self):
        tags = _parse_confluence_tags(["H1 OB bias (STRONG bearish)"])
        assert tags == ["ob_h1"]

    def test_m5_ob_maps_to_ob_m5(self):
        tags = _parse_confluence_tags(["M5 OB entry zone (1.0850–1.0855)"])
        assert tags == ["ob_m5"]

    def test_legacy_order_block_prefix(self):
        tags = _parse_confluence_tags(["Order block confirmed at zone"])
        assert tags == ["ob_h1"]

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

    def test_liquidity_sweep_detected(self):
        tags = _parse_confluence_tags(["Liquidity sweep detected (+8)"])
        assert tags == ["liquidity_sweep"]

    def test_liquidity_sweep_confirmed(self):
        tags = _parse_confluence_tags(["Liquidity sweep confirmed at entry zone"])
        assert tags == ["liquidity_sweep"]

    def test_liquidity_generic(self):
        tags = _parse_confluence_tags(["Liquidity event near zone"])
        assert tags == ["liquidity_sweep"]

    def test_volume_confirmed(self):
        tags = _parse_confluence_tags(["Volume confirmed (BULLISH, ratio=1.5x)"])
        assert tags == ["volume"]

    def test_volume_climax_not_mapped(self):
        tags = _parse_confluence_tags(["Volume climax WARNING (bearish_divergence)"])
        assert tags == []

    def test_inducement_detected(self):
        tags = _parse_confluence_tags(["Inducement detected (BULL_TRAP, +5)"])
        assert tags == ["inducement"]

    def test_wyckoff_spring(self):
        tags = _parse_confluence_tags(["Wyckoff SPRING (+5)"])
        assert tags == ["wyckoff"]

    def test_wyckoff_upthrust(self):
        tags = _parse_confluence_tags(["Wyckoff UPTHRUST (+5)"])
        assert tags == ["wyckoff"]

    def test_h1_and_m5_ob_both_mapped_separately(self):
        raw = ["H1 OB bias (STRONG)", "M5 OB entry zone (1.08)"]
        tags = _parse_confluence_tags(raw)
        assert set(tags) == {"ob_h1", "ob_m5"}

    def test_multiple_confluences_maps_all_12(self):
        raw = [
            "Structure aligned (STRONG)",
            "H1 OB bias (STRONG bearish)",
            "M5 OB entry zone (1.0850–1.0855)",
            "FVG entry zone (1.0840–1.0845)",
            "Session active (LONDON)",
            "News clear",
            "Currency strength (EUR strongest)",
            "Liquidity sweep detected (+8)",
            "Multi-TF FVG confluence",
            "Volume confirmed (BULLISH, ratio=1.5x)",
            "Inducement detected (BULL_TRAP, +5)",
            "Wyckoff SPRING (+5)",
        ]
        tags = _parse_confluence_tags(raw)
        assert set(tags) == set(CANONICAL_12)

    def test_unrecognized_string_skipped(self):
        tags = _parse_confluence_tags(["Something totally unknown"])
        assert tags == []

    def test_non_string_input_handled(self):
        tags = _parse_confluence_tags([42, None, True, "FVG entry zone"])
        assert tags == ["fvg"]

    def test_empty_list(self):
        assert _parse_confluence_tags([]) == []

    def test_m1_trigger_no_longer_mapped(self):
        tags = _parse_confluence_tags(["M1 confirmed: pin_bar — Bearish pin bar"])
        assert tags == []
        tags = _parse_confluence_tags(["M1 pattern detected at zone"])
        assert tags == []


# ──────────────────────────────────────────────────────────────────────────
# ScoringWeights — 12-factor schema
# ──────────────────────────────────────────────────────────────────────────


class TestScoringWeights12Factor:
    def test_factor_keys_count(self):
        assert len(FACTOR_KEYS) == 12

    def test_factor_keys_canonical(self):
        assert FACTOR_KEYS == CANONICAL_12

    def test_default_weights_sum_to_123(self):
        w = ScoringWeights()
        assert w.total == 123

    def test_as_dict_has_12_keys(self):
        d = ScoringWeights().as_dict()
        assert len(d) == 12
        assert set(d.keys()) == set(CANONICAL_12)

    def test_all_defaults_above_min_weight(self):
        d = ScoringWeights().as_dict()
        assert all(v >= ScoreOptimizer.MIN_WEIGHT for v in d.values())

    def test_default_baseline_magnitudes(self):
        w = ScoringWeights()
        assert w.structure_weight == 20
        assert w.ob_h1_weight == 10
        assert w.ob_m5_weight == 10
        assert w.fvg_weight == 15
        assert w.mtf_confluence_weight == 15
        assert w.session_weight == 10
        assert w.news_weight == 10
        assert w.currency_strength_weight == 10
        assert w.liquidity_sweep_weight == 8
        assert w.volume_weight == 5
        assert w.inducement_weight == 5
        assert w.wyckoff_weight == 5

    def test_optimize_returns_12_key_weights(self):
        random.seed(42)
        all_tags = list(CANONICAL_12)
        trades = []
        for _ in range(120):
            is_win = random.random() < 0.7
            pnl = round(random.uniform(5, 40), 2) if is_win else round(random.uniform(-30, -3), 2)
            tags = random.sample(all_tags, k=random.randint(3, 9))
            trades.append(
                {
                    "pnl": pnl,
                    "confluences_tags": tags,
                }
            )
        opt = ScoreOptimizer()
        result = opt.optimize(trades, min_trades=50)
        assert result.total == 123
        d = result.as_dict()
        assert len(d) == 12
        assert all(v >= ScoreOptimizer.MIN_WEIGHT for v in d.values())

    def test_factor_effectiveness_covers_all_12(self):
        all_tags = list(CANONICAL_12)
        trades = [{"pnl": 10.0, "confluences_tags": all_tags}] * 20
        eff = ScoreOptimizer().get_factor_effectiveness(trades)
        assert set(eff.keys()) == set(CANONICAL_12)

    def test_backward_compat_load_9_field_file(self, tmp_path):
        import json

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
        fp = str(tmp_path / "old_weights.json")
        with open(fp, "w") as f:
            json.dump(old_data, f)
        opt = ScoreOptimizer()
        loaded = opt.load_weights(fp)
        assert loaded.structure_weight == 17
        assert loaded.ob_h1_weight == 8
        assert loaded.ob_m5_weight == 9
        assert loaded.fvg_weight == 13
        assert loaded.volume_weight == 5
        assert loaded.inducement_weight == 5
        assert loaded.wyckoff_weight == 5
