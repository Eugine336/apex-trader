"""Tests for the active Consensus Division thesis (Phase 4 — the "big flip").

``form_thesis`` turns the weighted vote panel into an actionable thesis whose
``trigger`` flag is the market-driven, zone-independent entry signal. These are
dependency-light — pure consensus math, no pandas/torch required.
"""

import math

import pytest

from brain.directional_consensus import Vote, ConsensusThesis, form_thesis
from config import ConsensusConfig


_HA = ["currency_strength"]


def _thesis(votes, **over):
    kw = dict(
        min_net_score=1.5,
        min_agreement=0.55,
        high_authority_modules=_HA,
        high_authority_oppose_confidence=0.6,
        min_contributors=2,
        conviction_threshold=0.62,
        net_scale=0.0,
    )
    kw.update(over)
    return form_thesis(votes, **kw)


def test_strong_aligned_panel_triggers_long():
    votes = [
        Vote("structure", "LONG", 0.9, 3.0),
        Vote("currency_strength", "LONG", 0.8, 2.0),
        Vote("momentum", "LONG", 0.7, 1.0),
        Vote("volume", "LONG", 0.6, 1.0),
    ]
    t = _thesis(votes)
    assert isinstance(t, ConsensusThesis)
    assert t.direction == "LONG"
    assert t.trigger is True
    assert 0.0 < t.conviction <= 1.0
    assert "structure" in t.supporting_modules
    assert t.opposing_modules == []


def test_high_authority_opposition_blocks_trigger():
    votes = [
        Vote("structure", "LONG", 0.9, 3.0),
        Vote("currency_strength", "SHORT", 0.9, 2.0),
    ]
    t = _thesis(votes, min_contributors=1)
    assert t.direction == "NEUTRAL"
    assert t.trigger is False
    assert t.conviction == 0.0
    # The structured panel is preserved even on a NEUTRAL thesis.
    assert "currency_strength" in t.opposing_modules


def test_mixed_weak_panel_does_not_trigger():
    votes = [
        Vote("momentum", "LONG", 0.3, 1.0),
        Vote("volume", "SHORT", 0.3, 1.0),
    ]
    t = _thesis(votes)
    assert t.trigger is False


def test_conviction_threshold_is_a_real_knob():
    # A directional-but-not-decisive panel: clears a low threshold, fails a high one.
    votes = [
        Vote("structure", "LONG", 0.6, 1.0),
        Vote("momentum", "SHORT", 0.5, 1.0),
        Vote("volume", "LONG", 0.4, 1.0),
    ]
    low = _thesis(votes, min_net_score=0.1, min_agreement=0.5, conviction_threshold=0.1)
    high = _thesis(votes, min_net_score=0.1, min_agreement=0.5, conviction_threshold=0.99)
    assert low.direction in ("LONG", "SHORT")
    assert low.conviction == high.conviction  # same panel → same conviction
    assert low.trigger is True
    assert high.trigger is False  # threshold gates the same thesis


def test_empty_panel_is_neutral():
    t = _thesis([])
    assert t.direction == "NEUTRAL"
    assert t.conviction == 0.0
    assert t.trigger is False


def test_conviction_is_bounded_unit_interval():
    votes = [Vote("structure", "SHORT", 1.0, 5.0), Vote("momentum", "SHORT", 1.0, 5.0)]
    t = _thesis(votes, min_contributors=1)
    assert 0.0 <= t.conviction <= 1.0
    assert math.isfinite(t.conviction)


# ── Config wiring + validation ────────────────────────────────────────────

def test_consensus_config_active_trigger_defaults_off():
    cfg = ConsensusConfig()
    # Behaviour-neutral by default — the operator opts in to the market-driven trigger.
    assert cfg.active_trigger_enabled is False
    assert 0.0 <= cfg.conviction_threshold <= 1.0
    assert cfg.atr_period >= 1
    assert cfg.atr_sl_mult > 0 and cfg.atr_tp1_rr > 0 and cfg.atr_tp2_rr > 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("conviction_threshold", 1.5),
        ("conviction_threshold", -0.1),
        ("net_scale", -1.0),
        ("atr_period", 0),
        ("atr_sl_mult", 0.0),
        ("atr_tp1_rr", -1.0),
        ("trigger_cooldown_seconds", -5.0),
    ],
)
def test_consensus_config_rejects_invalid(field, value):
    with pytest.raises(ValueError):
        ConsensusConfig(**{field: value})
