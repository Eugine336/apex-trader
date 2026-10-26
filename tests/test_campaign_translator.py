"""Tests for the campaign translator (Phase G — origination from the Brain's spec)."""

import time
from types import SimpleNamespace

from cognition.campaign_translator import (
    OriginationIntent,
    compute_lot_size,
    derive_protective_levels,
    translate,
)
from cognition.identity import IDENTITY_STATEMENT, IDENTITY_VERSION
from cognition.contracts import CampaignSpecification


def _geometry(symbol, direction, entry, sl, tp):
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    return {
        "identity_version": IDENTITY_VERSION,
        "identity": IDENTITY_STATEMENT,
        "symbol": symbol,
        "direction": direction,
        "entry_price": entry,
        "stop_price": sl,
        "target_price": tp,
        "risk_distance": risk,
        "reward_distance": reward,
        "reward_r": reward / risk,
        "activated": True,
        "activation_reason": "synthetic completed-bar evidence for translator tests",
        "invalidation_reason": "synthetic structural invalidation",
        "evidence_ids": ["test-evidence-1", "test-evidence-2"],
        "entry_timeframe": "M5",
        "stop_timeframe": "M5",
        "target_timeframe": "M5",
        "evidence_timestamp_epoch": time.time(),
    }


def _initial_intent(symbol="EURUSD", direction="LONG", entry=1.1, sl=1.09, tp=1.12):
    return {
        "kind": "market", "entry_price": entry, "sl": sl, "tp": tp,
        "payoff_geometry": _geometry(symbol, direction, entry, sl, tp),
    }


def _spec(**kw) -> CampaignSpecification:
    base = dict(
        symbol="EURUSD", direction="LONG", desired_exposure=0.5,
        confidence=0.8, thesis="momentum breakout",
        decision_id="decision-default", opportunity_id="opportunity-default",
        opportunity_state="ACTIVE",
    )
    base.update(kw)
    if "initial_execution_intent" not in kw:
        if base["direction"] == "SHORT":
            base["initial_execution_intent"] = _initial_intent(
                base["symbol"], "SHORT", entry=1.1, sl=1.11, tp=1.08,
            )
        elif base["direction"] == "LONG":
            base["initial_execution_intent"] = _initial_intent(
                base["symbol"], "LONG", entry=1.1, sl=1.09, tp=1.12,
            )
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
    spec = _spec(initial_execution_intent=_initial_intent(
        entry=1.1, sl=1.08, tp=1.12,
    ))
    intent = translate(spec, balance=5_000.0)
    assert intent.sl == 1.0800
    assert intent.tp == 1.0950


def test_translate_carries_provenance():
    spec = _spec(
        campaign_id="c123", decision_id="d456", opportunity_id="o789",
    )
    intent = translate(spec, balance=5_000.0)
    assert intent.campaign_id == "c123"
    assert intent.decision_id == "d456"
    assert intent.to_dict()["campaign_id"] == "c123"
    from cognition.identity import IDENTITY_STATEMENT, IDENTITY_VERSION
    assert intent.identity_version == IDENTITY_VERSION
    assert intent.identity == IDENTITY_STATEMENT
    assert intent.to_dict()["identity_version"] == IDENTITY_VERSION
    assert intent.to_dict()["identity"] == IDENTITY_STATEMENT
    assert intent.opportunity_id == spec.opportunity_id
    assert intent.to_dict()["opportunity_id"] == spec.opportunity_id


def test_translate_rejects_noncanonical_identity_instead_of_rewriting_it():
    assert translate(_spec(identity_version="old-version")) is None
    assert translate(_spec(identity="competing identity")) is None


def test_translate_requires_decision_campaign_and_activated_opportunity_links():
    assert translate(_spec(decision_id="")) is None
    assert translate(_spec(opportunity_id="")) is None
    no_campaign = SimpleNamespace(
        symbol="EURUSD", direction="LONG", desired_exposure=0.5,
        decision_id="d1", opportunity_id="o1", opportunity_state="ACTIVE",
        identity_version=IDENTITY_VERSION, identity=IDENTITY_STATEMENT,
    )
    assert translate(no_campaign) is None
    assert translate(_spec(opportunity_state="FORMING")) is None
    assert translate(_spec(opportunity_state="UNKNOWN")) is None


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


# ── compute_lot_size (broker-valid, conviction-scaled sizing) ─────────────────

def test_lot_size_risk_based():
    # risk $80, stop 8.0, tick_value 1 per 0.01 → loss/lot 800 → 0.1 lot
    lots = compute_lot_size(risk_usd=80, stop_distance=8.0, tick_value=1.0,
                            tick_size=0.01, vol_min=0.01, vol_step=0.01)
    assert abs(lots - 0.1) < 1e-9


def test_lot_size_rounds_down_to_step():
    # 0.137 → floor to 0.01 step → 0.13
    lots = compute_lot_size(risk_usd=109.6, stop_distance=8.0, tick_value=1.0,
                            tick_size=0.01, vol_min=0.01, vol_step=0.01)
    assert lots == 0.13


def test_lot_size_floors_to_min_when_harvesting():
    # sub-min risk lot on a 0.5-min instrument (Brent) → floored to 0.5
    lots = compute_lot_size(risk_usd=2, stop_distance=1.0, tick_value=1.0,
                            tick_size=0.01, vol_min=0.5, vol_step=0.5)
    assert lots == 0.5


def test_lot_size_declines_when_harvest_off():
    lots = compute_lot_size(risk_usd=2, stop_distance=1.0, tick_value=1.0,
                            tick_size=0.01, vol_min=0.5, vol_step=0.5, floor_to_min=False)
    assert lots == 0.0   # never send an under-min order


def test_lot_size_respects_cap():
    lots = compute_lot_size(risk_usd=800, stop_distance=8.0, tick_value=1.0,
                            tick_size=0.01, vol_min=0.01, vol_step=0.01, max_lots=0.05)
    assert lots == 0.05


def test_lot_size_never_zero_to_broker_and_fault_safe():
    # unusable inputs but harvesting on → valid min lot, never 0-through-to-broker
    assert compute_lot_size(risk_usd=0, stop_distance=0, tick_value=0, tick_size=0,
                            vol_min=0.01, vol_step=0.01) == 0.01
    # unusable + harvest off → 0 (caller skips, does not send)
    assert compute_lot_size(risk_usd=0, stop_distance=0, tick_value=0, tick_size=0,
                            vol_min=0.01, vol_step=0.01, floor_to_min=False) == 0.0
