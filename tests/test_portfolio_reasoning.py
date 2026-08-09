"""Portfolio-level reasoning (Articles XXVII / XXVIII) — the Brain reasons across
the WHOLE book at once, not only per-symbol.

Covers :meth:`~cognition.brain.CognitiveBrain.reason_portfolio`: EV ranking,
correlated-concentration demotion, total-exposure demotion, and the no-op path
when opportunities are uncorrelated and within budget.
"""

from cognition.brain import CognitiveBrain, BrainOutput
from cognition.contracts import CampaignSpecification, DecisionPackage, DecisionType


def _open_output(symbol, direction, ev, exposure):
    decision = DecisionPackage(
        symbol=symbol, decision_type=DecisionType.OPEN_CAMPAIGN,
        thesis=f"{direction} {symbol}", confidence=0.8, expected_value=ev,
        campaign_recommendation=f"open {direction}",
    )
    campaign = CampaignSpecification(
        symbol=symbol, direction=direction, desired_exposure=exposure,
        expected_value=ev, confidence=0.8, decision_id=decision.decision_id,
    )
    return BrainOutput(decision=decision, campaign=campaign, direction=direction)


def _brain(**kw):
    return CognitiveBrain(reasoner=None, **kw)


def test_portfolio_ranks_by_ev():
    brain = _brain(max_total_exposure=10.0)
    # Distinct asset classes + small exposure so only ranking is exercised.
    outs = [
        _open_output("EURUSD", "LONG", ev=0.5, exposure=0.3),
        _open_output("BTCUSD", "LONG", ev=1.5, exposure=0.3),
        _open_output("XAUUSD", "LONG", ev=1.0, exposure=0.3),
    ]
    result = brain.reason_portfolio(outs, existing_positions=[])
    evs = [o.decision.expected_value for o in result]
    assert evs == sorted(evs, reverse=True)
    assert evs == [1.5, 1.0, 0.5]
    # Nothing demoted.
    assert all(o.decision.decision_type == DecisionType.OPEN_CAMPAIGN for o in result)


def test_portfolio_concentration_limit():
    brain = _brain(max_correlated_positions=3, max_total_exposure=10.0)
    outs = [
        _open_output("BTCUSD", "LONG", ev=1.4, exposure=0.2),
        _open_output("ETHUSD", "LONG", ev=1.3, exposure=0.2),
        _open_output("SOLUSD", "LONG", ev=1.2, exposure=0.2),
        _open_output("XRPUSD", "LONG", ev=1.1, exposure=0.2),  # 4th crypto LONG
    ]
    result = brain.reason_portfolio(outs, existing_positions=[])
    by_symbol = {o.decision.symbol: o for o in result}
    # The three highest-EV crypto longs survive; the 4th is demoted.
    assert by_symbol["BTCUSD"].decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert by_symbol["ETHUSD"].decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert by_symbol["SOLUSD"].decision.decision_type == DecisionType.OPEN_CAMPAIGN
    demoted = by_symbol["XRPUSD"]
    assert demoted.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert demoted.campaign is None
    assert "concentration" in demoted.decision.risk_rationale.lower()


def test_portfolio_exposure_cap():
    brain = _brain(max_correlated_positions=10, max_total_exposure=3.0)
    # Distinct asset classes (no concentration demotion); total exposure 4.0 > 3.0.
    outs = [
        _open_output("BTCUSD", "LONG", ev=1.4, exposure=1.0),
        _open_output("EURUSD", "LONG", ev=1.3, exposure=1.0),
        _open_output("XAUUSD", "LONG", ev=1.2, exposure=1.0),
        _open_output("SPX500", "LONG", ev=1.1, exposure=1.0),  # lowest EV
    ]
    result = brain.reason_portfolio(outs, existing_positions=[])
    by_symbol = {o.decision.symbol: o for o in result}
    # Lowest-EV opportunity demoted to bring total exposure within budget.
    assert by_symbol["SPX500"].decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert by_symbol["SPX500"].campaign is None
    survivors = [s for s in ("BTCUSD", "EURUSD", "XAUUSD")
                 if by_symbol[s].decision.decision_type == DecisionType.OPEN_CAMPAIGN]
    assert len(survivors) == 3
    total = sum(by_symbol[s].campaign.desired_exposure for s in survivors)
    assert total <= 3.0


def test_portfolio_no_change_when_uncorrelated():
    brain = _brain(max_correlated_positions=3, max_total_exposure=3.0)
    outs = [
        _open_output("BTCUSD", "LONG", ev=1.4, exposure=0.5),
        _open_output("EURUSD", "LONG", ev=1.3, exposure=0.5),
        _open_output("XAUUSD", "LONG", ev=1.2, exposure=0.5),
    ]
    result = brain.reason_portfolio(outs, existing_positions=[])
    assert all(o.decision.decision_type == DecisionType.OPEN_CAMPAIGN for o in result)
    assert all(o.campaign is not None for o in result)
