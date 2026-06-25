"""Phase 3 — probabilistic evidence model for ``compute_bias``.

Verifies the weighted, veto-free evidence aggregation: every timeframe
contributes ``weight × confidence`` toward LONG/SHORT, higher timeframes weigh
more but cannot veto a stronger lower-timeframe consensus, developing structure
adds discounted evidence, and the backward-compatible fields
(``direction``/``score``/``strength``/``tradeable``/per-TF trends) stay aligned
with the old hierarchy's semantics for typical conditions.
"""

from types import SimpleNamespace

from brain.decision_core import blend_concepts, compute_bias
from brain.structure_engine import StructureAnalysis, StructureEvent, Trend


def _sa(trend: str, confidence: float) -> StructureAnalysis:
    return StructureAnalysis(
        trend=Trend(trend),
        last_event=StructureEvent.NONE,
        swing_high=None,
        swing_low=None,
        last_bos_level=None,
        last_choch_level=None,
        structure_broken=False,
        bullish_swing_points=[],
        bearish_swing_points=[],
        confidence=confidence,
    )


# ── Single timeframe ────────────────────────────────────────────────────────


def test_single_tf_bullish():
    bias = compute_bias({"H4": _sa("BULLISH", 0.8)})
    assert bias["direction"] == "LONG"
    assert bias["long_probability"] > 0.0
    assert bias["short_probability"] == 0.0


def test_single_tf_bearish():
    bias = compute_bias({"H1": _sa("BEARISH", 0.8)})
    assert bias["direction"] == "SHORT"
    assert bias["short_probability"] > 0.0
    assert bias["long_probability"] == 0.0


# ── Unanimous agreement ──────────────────────────────────────────────────────


def test_all_tfs_agree_long():
    bias = compute_bias(
        {
            "D1": _sa("BULLISH", 0.8),
            "H4": _sa("BULLISH", 0.8),
            "H1": _sa("BULLISH", 0.8),
            "M15": _sa("BULLISH", 0.8),
            "M5": _sa("BULLISH", 0.8),
        }
    )
    assert bias["direction"] == "LONG"
    assert bias["strength"] == "STRONG"
    assert bias["tradeable"] is True
    assert bias["long_probability"] > 0.7
    assert bias["conflict_score"] < 0.05


def test_all_tfs_agree_short():
    bias = compute_bias(
        {
            "H4": _sa("BEARISH", 0.8),
            "H1": _sa("BEARISH", 0.8),
            "M15": _sa("BEARISH", 0.8),
        }
    )
    assert bias["direction"] == "SHORT"
    assert bias["strength"] == "STRONG"
    assert bias["tradeable"] is True


# ── Conflict ──────────────────────────────────────────────────────────────────


def test_h4_h1_conflict():
    """Equal-confidence H4 vs H1 opposition → high conflict, not tradeable."""
    bias = compute_bias({"H4": _sa("BULLISH", 0.8), "H1": _sa("BEARISH", 0.8)})
    assert bias["strength"] == "CONFLICTED"
    assert bias["tradeable"] is False
    assert bias["direction"] == ""  # backward-compat: no actionable direction
    # Raw probabilities still exposed for the opportunity engine.
    assert bias["long_probability"] > 0.0
    assert bias["short_probability"] > 0.0


def test_no_tf_has_veto():
    """A high-conf H4 cannot veto a confident lower-TF majority."""
    bias = compute_bias(
        {
            "H4": _sa("BEARISH", 0.9),   # strategic context, opposing
            "H1": _sa("BULLISH", 0.6),
            "M15": _sa("BULLISH", 0.6),
            "M5": _sa("BULLISH", 0.6),
        }
    )
    assert bias["direction"] == "LONG"
    assert bias["long_probability"] > bias["short_probability"]


def test_weights_matter():
    """Same confidence, opposite directions → the heavier TF wins."""
    bias = compute_bias({"H1": _sa("BULLISH", 0.8), "M5": _sa("BEARISH", 0.8)})
    assert bias["direction"] == "LONG"  # H1 (0.25) outweighs M5 (0.15)


# ── Ranging / empty ───────────────────────────────────────────────────────────


def test_ranging_excluded():
    """RANGING frames contribute no evidence and don't dilute the result."""
    only_h1 = compute_bias({"H1": _sa("BULLISH", 0.8)})
    with_ranging = compute_bias(
        {"H4": _sa("RANGING", 0.9), "H1": _sa("BULLISH", 0.8)}
    )
    assert with_ranging["direction"] == "LONG"
    assert with_ranging["long_probability"] == only_h1["long_probability"]


def test_empty_struct():
    bias = compute_bias({})
    assert bias["direction"] == ""
    assert bias["strength"] == "NONE"
    assert bias["tradeable"] is False
    assert bias["long_probability"] == 0.0
    assert bias["short_probability"] == 0.0
    assert bias["conflict_score"] == 0.0


# ── Developing structure ──────────────────────────────────────────────────────


def test_developing_increases_conviction():
    confirmed = {"H4": _sa("BULLISH", 0.8), "H1": _sa("BULLISH", 0.8)}
    base = compute_bias(confirmed)
    blended = compute_bias(confirmed, developing_struct_by_tf={"H1": _sa("BULLISH", 0.9)})
    assert blended["direction"] == "LONG"
    assert blended["long_probability"] > base["long_probability"]
    assert blended["developing_blend"] > 0.0


def test_developing_opposes_confirmed():
    confirmed = {"H4": _sa("BULLISH", 0.8), "H1": _sa("BULLISH", 0.8)}
    base = compute_bias(confirmed)
    blended = compute_bias(confirmed, developing_struct_by_tf={"H1": _sa("BEARISH", 0.9)})
    assert blended["direction"] == "LONG"  # confirmed majority still wins
    assert blended["confidence"] < base["confidence"]
    assert blended["developing_blend"] < 0.0


def test_developing_discount_applied():
    """Discounted developing evidence cannot overturn an equal-confidence
    confirmed vote (it would tie at full weight)."""
    bias = compute_bias(
        {"H1": _sa("BULLISH", 1.0)},
        developing_struct_by_tf={"H1": _sa("BEARISH", 1.0)},
    )
    assert bias["direction"] == "LONG"
    assert bias["long_probability"] > bias["short_probability"]


# ── Conflict score ─────────────────────────────────────────────────────────────


def test_conflict_score_zero_when_unanimous():
    bias = compute_bias({"H4": _sa("BULLISH", 0.8), "H1": _sa("BULLISH", 0.8)})
    assert bias["conflict_score"] == 0.0


def test_conflict_score_high_when_split():
    """Balanced opposing evidence → conflict_score ≈ 1.0."""
    # H4 raw = 0.20 × 0.8 = 0.16; H1 raw = 0.25 × 0.64 = 0.16 → perfectly even.
    bias = compute_bias({"H4": _sa("BULLISH", 0.8), "H1": _sa("BEARISH", 0.64)})
    assert bias["conflict_score"] > 0.9


# ── Backward compatibility ──────────────────────────────────────────────────


def test_backward_compat_tradeable():
    """tradeable is True iff strength is STRONG or MODERATE."""
    aligned = compute_bias({"H4": _sa("BULLISH", 0.8), "H1": _sa("BULLISH", 0.8)})
    assert aligned["tradeable"] == (aligned["strength"] in ("STRONG", "MODERATE"))

    conflicted = compute_bias({"H4": _sa("BULLISH", 0.8), "H1": _sa("BEARISH", 0.8)})
    assert conflicted["tradeable"] == (conflicted["strength"] in ("STRONG", "MODERATE"))
    assert conflicted["tradeable"] is False


def test_backward_compat_direction_threshold():
    """A sub-threshold probability edge yields no actionable direction."""
    bias = compute_bias({"H4": _sa("BULLISH", 0.8), "H1": _sa("BEARISH", 0.64)})
    assert abs(bias["long_probability"] - bias["short_probability"]) < 0.05
    assert bias["direction"] == ""


def test_backward_compat_per_tf_trends():
    bias = compute_bias(
        {
            "H4": _sa("BULLISH", 0.8),
            "H1": _sa("BEARISH", 0.8),
            "D1": _sa("RANGING", 0.5),
        }
    )
    assert bias["h4_trend"] == "BULLISH"
    assert bias["h1_trend"] == "BEARISH"
    assert bias["d1_trend"] == "RANGING"
    # Missing TF → UNKNOWN.
    assert compute_bias({})["h4_trend"] == "UNKNOWN"


def test_d1_aligned():
    aligned = compute_bias(
        {"H4": _sa("BULLISH", 0.8), "H1": _sa("BULLISH", 0.8), "D1": _sa("BULLISH", 0.8)}
    )
    assert aligned["direction"] == "LONG"
    assert aligned["d1_aligned"] is True

    opposed = compute_bias(
        {"H4": _sa("BULLISH", 0.8), "H1": _sa("BULLISH", 0.8), "D1": _sa("BEARISH", 0.3)}
    )
    assert opposed["direction"] == "LONG"
    assert opposed["d1_aligned"] is False


def test_blend_concepts_still_works():
    """blend_concepts applied after compute_bias nudges score, direction kept."""
    bias = compute_bias({"H4": _sa("BULLISH", 0.8), "H1": _sa("BULLISH", 0.8)})
    base_score = bias["score"]
    long_sig = SimpleNamespace(
        is_directional=True, direction="LONG", strength=1.0, name="fvg",
    )
    blended = blend_concepts(bias, {"H1": [long_sig]}, {"H1": "TREND"})
    assert blended["direction"] == "LONG"
    assert blended["score"] >= base_score  # agreeing concept boosts (or holds at cap)
    assert blended["concept_direction"] == "LONG"


def test_developing_blend_field():
    confirmed = {"H4": _sa("BULLISH", 0.8), "H1": _sa("BULLISH", 0.8)}
    # No developing → exactly 0.0.
    assert compute_bias(confirmed)["developing_blend"] == 0.0
    # Agreeing developing → positive shift.
    blended = compute_bias(confirmed, developing_struct_by_tf={"H1": _sa("BULLISH", 0.9)})
    assert blended["developing_blend"] != 0.0
