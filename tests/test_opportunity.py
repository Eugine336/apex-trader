"""Tests for cognition.opportunity — net-EV opportunity qualification (Part XIX)."""

from cognition.opportunity import qualify_net_ev, OpportunityQualification


def test_tiny_move_rejected_by_cost():
    # Gold-style 0.01 lot (money_per_price=1.0): a +0.4 target vs 2.0 stop, and a
    # ~0.9 round-trip cost. Movement, but not opportunity — must be rejected.
    q = qualify_net_ev(
        entry=4268.0, sl=4266.0, tp=4268.4, confidence=0.66,
        money_per_price=1.0, cost_ccy=0.9,
    )
    assert isinstance(q, OpportunityQualification)
    assert q.qualified is False
    assert q.expected_net < 0.0


def test_real_move_qualifies():
    q = qualify_net_ev(
        entry=4268.0, sl=4262.0, tp=4281.0, confidence=0.72,
        money_per_price=1.0, cost_ccy=0.9,
    )
    assert q.qualified is True
    assert q.expected_net > 0.0


def test_cost_multiple_doubling_flips_marginal_trade():
    # A trade that is marginally positive at 1× cost must be refused at 2× cost
    # (Art 8: "would I still take this if costs doubled?").
    base = dict(entry=100.0, sl=99.0, tp=101.0, confidence=0.6, money_per_price=1.0)
    # gross = 0.6*1.0 - 0.4*1.0 = 0.20; with cost 0.15:
    at_1x = qualify_net_ev(**base, cost_ccy=0.15, cost_multiple=1.0)
    at_2x = qualify_net_ev(**base, cost_ccy=0.15, cost_multiple=2.0)
    assert at_1x.qualified is True      # 0.20 - 0.15 = +0.05
    assert at_2x.qualified is False     # 0.20 - 0.30 = -0.10


def test_confidence_scales_expected_value():
    lo = qualify_net_ev(entry=100, sl=98, tp=104, confidence=0.30,
                        money_per_price=1.0, cost_ccy=0.0, cost_multiple=1.0)
    hi = qualify_net_ev(entry=100, sl=98, tp=104, confidence=0.90,
                        money_per_price=1.0, cost_ccy=0.0, cost_multiple=1.0)
    assert hi.expected_gross > lo.expected_gross


def test_min_edge_gate():
    # Positive but small net EV rejected when a minimum edge is required.
    q = qualify_net_ev(entry=100, sl=99, tp=102, confidence=0.6, money_per_price=1.0,
                       cost_ccy=0.0, cost_multiple=1.0, min_edge_ccy=1.0)
    # gross = 0.6*2 - 0.4*1 = 0.8; net 0.8 <= min_edge 1.0 → reject
    assert q.qualified is False


def test_noncomputable_inputs_are_rejected_not_raised():
    for bad in (
        dict(entry=0.0, sl=99.0, tp=101.0),      # no entry
        dict(entry=100.0, sl=100.0, tp=101.0),   # zero risk
        dict(entry=100.0, sl=99.0, tp=100.0),    # zero reward
    ):
        q = qualify_net_ev(confidence=0.6, money_per_price=1.0, cost_ccy=0.5, **bad)
        assert q.qualified is False
    # invalid types never raise
    q = qualify_net_ev(entry="x", sl=1, tp=2, confidence=0.5,
                       money_per_price=1.0, cost_ccy=0.1)
    assert q.qualified is False


def test_zero_money_per_price_rejected():
    q = qualify_net_ev(entry=100, sl=99, tp=102, confidence=0.7,
                       money_per_price=0.0, cost_ccy=0.1)
    assert q.qualified is False
