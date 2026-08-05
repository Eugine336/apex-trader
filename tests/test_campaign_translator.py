"""Tests for the campaign translator (Phase G — origination from the Brain's spec)."""

from cognition.campaign_translator import OriginationIntent, translate
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
