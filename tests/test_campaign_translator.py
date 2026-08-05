"""Tests for the campaign translator (Phase G — origination from the Brain's spec)."""

from cognition.campaign_translator import (
    OriginationIntent,
    derive_protective_levels,
    translate,
)
from cognition.contracts import CampaignSpecification


def _spec(**kw) -> CampaignSpecification:
    base = dict(symbol="EURUSD", direction="LONG", desired_exposure=0.5,
                confidence=0.8, thesis="momentum breakout")
    base.update(kw)
    return CampaignSpecification(**base)


def test_translate_long_sizes_stake_from_balance():
    intent = translate(_spec(), balance=10_000.0, risk_fraction=0.01, max_exposure=1.0)
    assert isinstance(intent, OriginationIntent)
    assert intent.symbol == "EURUSD"
    assert intent.direction == "LONG"
    # stake = balance × risk × exposure = 10000 × 0.01 × 0.5
    assert abs(intent.stake_usd - 50.0) < 1e-6
    assert intent.source == "ai_brain"


def test_translate_short_direction_preserved():
    intent = translate(_spec(direction="SHORT"), balance=1_000.0)
    assert intent is not None
    assert intent.direction == "SHORT"


def test_translate_flat_returns_none():
    assert translate(_spec(direction="FLAT")) is None


def test_translate_empty_symbol_returns_none():
    assert translate(_spec(symbol="")) is None


def test_translate_no_balance_leaves_stake_none():
    intent = translate(_spec(), balance=0.0)
    assert intent is not None
    assert intent.stake_usd is None


def test_translate_max_exposure_caps_exposure():
    intent = translate(_spec(desired_exposure=0.9), balance=10_000.0,
                       risk_fraction=0.02, max_exposure=0.25)
    assert intent is not None
    assert intent.exposure == 0.25
    # stake reflects the capped exposure: 10000 × 0.02 × 0.25
    assert abs(intent.stake_usd - 50.0) < 1e-6


def test_translate_carries_stop_and_target_from_intent():
    spec = _spec(initial_execution_intent={"sl": 1.0800, "tp": 1.0950})
    intent = translate(spec, balance=5_000.0)
    assert intent.sl == 1.0800
    assert intent.tp == 1.0950


def test_translate_carries_provenance():
    spec = _spec(campaign_id="c123", decision_id="d456")
    intent = translate(spec, balance=5_000.0)
    assert intent.campaign_id == "c123"
    assert intent.decision_id == "d456"
    assert intent.to_dict()["campaign_id"] == "c123"


def test_translate_never_raises_on_garbage():
    assert translate(object()) is None
    assert translate(None) is None


# ── derive_protective_levels (Part X deterministic stop/target) ────────────────

def test_derive_long_default_fraction_and_reward():
    sl, tp = derive_protective_levels("LONG", 2000.0)  # 0.4% stop, 2x reward
    assert abs(sl - 1992.0) < 1e-6
    assert abs(tp - 2016.0) < 1e-6
    assert sl < 2000.0 < tp


def test_derive_short_is_mirrored():
    sl, tp = derive_protective_levels("SHORT", 2000.0)
    assert abs(sl - 2008.0) < 1e-6
    assert abs(tp - 1984.0) < 1e-6
    assert tp < 2000.0 < sl


def test_derive_prefers_structural_target_far_enough_ahead():
    # 2005 is only +5 (< risk*min_rr=8) so it's skipped; 2050 qualifies.
    sl, tp = derive_protective_levels("LONG", 2000.0, structural_targets=[2005.0, 2050.0])
    assert tp == 2050.0


def test_derive_falls_back_to_reward_multiple_when_no_structure():
    sl, tp = derive_protective_levels("LONG", 2000.0, structural_targets=[])
    assert abs(tp - 2016.0) < 1e-6


def test_derive_instrument_agnostic_for_fx():
    sl, tp = derive_protective_levels("LONG", 1.08)
    assert sl < 1.08 < tp


def test_derive_rejects_invalid_inputs():
    assert derive_protective_levels("FLAT", 2000.0) == (None, None)
    assert derive_protective_levels("LONG", 0.0) == (None, None)
    assert derive_protective_levels("LONG", 2000.0, stop_fraction=0.0) == (None, None)
    assert derive_protective_levels("LONG", "x") == (None, None)  # never raises
