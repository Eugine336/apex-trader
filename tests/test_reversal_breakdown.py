"""
Reversal-vs-continuation realized EV breakdown (roadmap E tracking).

Reversal trades are tagged at entry with the REVERSAL_TRADE confluence;
_summarize_reversal_split groups journal trades and reports count / win-rate /
EV (count-weighted mean R) so the counter-trend reversal book can be judged
separately from continuation trades.
"""

from dashboard.state_performance import _summarize_reversal_split


def _trade(pnl_dollars, risk_dollars=10.0, reversal=False, pnl=None):
    conf = ["Structure aligned"]
    if reversal:
        conf.append("REVERSAL_TRADE")
    return {
        "confluences_raw": conf,
        "pnl_dollars": pnl_dollars,
        "risk_dollars": risk_dollars,
        "pnl": pnl if pnl is not None else pnl_dollars,
    }


def test_splits_reversal_from_continuation():
    trades = [
        _trade(20.0, reversal=True),
        _trade(-10.0, reversal=True),
        _trade(15.0, reversal=False),
        _trade(15.0, reversal=False),
        _trade(15.0, reversal=False),
    ]
    out = _summarize_reversal_split(trades)
    assert out["reversal"]["count"] == 2
    assert out["continuation"]["count"] == 3
    assert out["reversal"]["wins"] == 1
    assert out["reversal"]["losses"] == 1


def test_reversal_ev_is_count_weighted_r():
    # +2.0R and -1.0R → EV = +0.5R; win rate 50%.
    trades = [
        _trade(20.0, risk_dollars=10.0, reversal=True),
        _trade(-10.0, risk_dollars=10.0, reversal=True),
    ]
    out = _summarize_reversal_split(trades)
    rev = out["reversal"]
    assert rev["ev_r"] == 0.5
    assert rev["ev_samples"] == 2
    assert rev["win_rate"] == 50.0
    assert rev["total_pnl"] == 10.0
    assert rev["avg_pnl"] == 5.0


def test_no_reversals_yields_empty_reversal_bucket():
    out = _summarize_reversal_split([_trade(5.0), _trade(-3.0)])
    assert out["reversal"]["count"] == 0
    assert out["reversal"]["ev_r"] == 0.0
    assert out["continuation"]["count"] == 2


def test_missing_risk_dollars_excluded_from_ev_but_counted():
    trades = [_trade(8.0, risk_dollars=None, reversal=True)]
    out = _summarize_reversal_split(trades)
    rev = out["reversal"]
    assert rev["count"] == 1
    assert rev["wins"] == 1
    assert rev["ev_samples"] == 0      # no R sample (risk unknown)
    assert rev["ev_r"] == 0.0
    assert rev["avg_pnl"] == 8.0       # dollar P&L still reported


def test_empty_input():
    out = _summarize_reversal_split([])
    assert out["reversal"]["count"] == 0
    assert out["continuation"]["count"] == 0
