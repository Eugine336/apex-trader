"""Tests for execution-side verified expected-return qualification."""

import pytest

from cognition.opportunity import qualify_net_ev, OpportunityQualification


def test_tiny_move_rejected_by_cost():
    q = qualify_net_ev(expected_return_r=0.2, risk_ccy=2.0, cost_ccy=0.9)
    assert isinstance(q, OpportunityQualification)
    assert q.qualified is False
    assert q.expected_net < 0.0


def test_real_move_qualifies():
    q = qualify_net_ev(expected_return_r=0.8, risk_ccy=6.0, cost_ccy=0.9)
    assert q.qualified is True
    assert q.expected_net > 0.0


def test_cost_multiple_doubling_flips_marginal_trade():
    base = dict(expected_return_r=0.2, risk_ccy=1.0, cost_ccy=0.15)
    at_1x = qualify_net_ev(**base, cost_multiple=1.0)
    at_2x = qualify_net_ev(**base, cost_multiple=2.0)
    assert at_1x.qualified is True
    assert at_1x.expected_net == pytest.approx(0.05)
    assert at_2x.qualified is False
    assert at_2x.expected_net == pytest.approx(-0.1)


def test_expected_return_scales_with_verified_risk_currency():
    one_r = qualify_net_ev(expected_return_r=0.5, risk_ccy=1.0, cost_ccy=0.0, cost_multiple=1.0)
    two_r = qualify_net_ev(expected_return_r=0.5, risk_ccy=2.0, cost_ccy=0.0, cost_multiple=1.0)
    assert two_r.expected_gross > one_r.expected_gross
    assert two_r.expected_gross == 1.0


def test_min_edge_gate():
    q = qualify_net_ev(expected_return_r=0.8, risk_ccy=1.0, cost_ccy=0.0, cost_multiple=1.0, min_edge_ccy=1.0)
    assert q.qualified is False


def test_noncomputable_inputs_are_rejected_not_raised():
    for bad in (
        dict(expected_return_r=0.5, risk_ccy=0.0, cost_ccy=0.1),
        dict(expected_return_r=float("nan"), risk_ccy=1.0, cost_ccy=0.1),
        dict(expected_return_r=0.5, risk_ccy=1.0, cost_ccy=float("inf")),
        dict(expected_return_r=0.5, risk_ccy=1.0, cost_ccy=-0.1),
    ):
        assert qualify_net_ev(**bad).qualified is False


def test_zero_cost_and_nonpositive_expected_return_are_handled():
    assert qualify_net_ev(expected_return_r=0.0, risk_ccy=1.0, cost_ccy=0.0).qualified is False
    assert qualify_net_ev(expected_return_r=-0.2, risk_ccy=1.0, cost_ccy=0.0).qualified is False


def test_confidence_is_not_an_accepted_probability_argument():
    with pytest.raises(TypeError):
        qualify_net_ev(confidence=0.99, risk_ccy=100.0, cost_ccy=0.0)
