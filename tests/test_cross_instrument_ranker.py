"""APEX TRADER — Cross-Instrument Ranker tests (GAP 2).

Covers: spread-cost EV penalty, per-pair win-rate adjustment, best-first ordering
across instruments, side-effect annotation of ``adjusted_ev``, runtime lookup
binding, robustness to failing/absent hooks, and empty/single-item inputs.
"""

from dataclasses import dataclass

from brain.cross_instrument_ranker import CrossInstrumentRanker


@dataclass
class _Cand:
    symbol: str
    direction: str = "LONG"
    ev: float = 0.0
    confidence: float = 0.5
    adjusted_ev: float = 0.0


def test_no_lookups_preserves_raw_ev_order():
    r = CrossInstrumentRanker()
    cands = [_Cand("EURUSD", ev=0.5), _Cand("GBPJPY", ev=1.2), _Cand("XAUUSD", ev=0.8)]
    ranked = r.rank(cands)
    assert [c.symbol for c in ranked] == ["GBPJPY", "XAUUSD", "EURUSD"]
    # adjusted_ev is set even without lookups (== raw ev).
    assert ranked[0].adjusted_ev == 1.2


def test_spread_penalty_docks_ev():
    spreads = {"EURUSD": 0.0, "GBPJPY": 30.0}
    r = CrossInstrumentRanker(
        spread_pips_lookup=lambda s: spreads.get(s, 0.0),
        spread_ev_penalty_per_pip=0.02,
    )
    cands = [_Cand("EURUSD", ev=1.0), _Cand("GBPJPY", ev=1.2)]
    ranked = r.rank(cands)
    # GBPJPY 1.2 - 0.02*30 = 0.6 < EURUSD 1.0 → EURUSD now wins.
    assert ranked[0].symbol == "EURUSD"
    assert abs(ranked[1].adjusted_ev - 0.6) < 1e-9


def test_win_rate_adjustment():
    wr = {"EURUSD": 0.7, "GBPJPY": 0.3}
    r = CrossInstrumentRanker(
        win_rate_lookup=lambda s: wr.get(s),
        winrate_ev_weight=1.0,
    )
    cands = [_Cand("EURUSD", ev=1.0), _Cand("GBPJPY", ev=1.0)]
    ranked = r.rank(cands)
    # EURUSD +0.2, GBPJPY -0.2 → EURUSD first.
    assert ranked[0].symbol == "EURUSD"
    assert abs(ranked[0].adjusted_ev - 1.2) < 1e-9
    assert abs(ranked[1].adjusted_ev - 0.8) < 1e-9


def test_win_rate_none_is_neutral():
    r = CrossInstrumentRanker(win_rate_lookup=lambda s: None, winrate_ev_weight=1.0)
    c = _Cand("EURUSD", ev=0.9)
    assert abs(r.adjusted_ev(c) - 0.9) < 1e-9


def test_failing_spread_hook_is_neutral():
    def _boom(_s):
        raise RuntimeError("broker down")

    r = CrossInstrumentRanker(spread_pips_lookup=_boom)
    c = _Cand("EURUSD", ev=0.75)
    assert abs(r.adjusted_ev(c) - 0.75) < 1e-9


def test_bind_lookups_late():
    r = CrossInstrumentRanker(spread_ev_penalty_per_pip=0.02)
    r.bind_lookups(spread_pips_lookup=lambda s: 10.0)
    c = _Cand("EURUSD", ev=1.0)
    assert abs(r.adjusted_ev(c) - 0.8) < 1e-9


def test_empty_and_single():
    r = CrossInstrumentRanker()
    assert r.rank([]) == []
    one = [_Cand("EURUSD", ev=0.4)]
    out = r.rank(one)
    assert len(out) == 1 and out[0].adjusted_ev == 0.4


def test_tie_break_on_confidence():
    r = CrossInstrumentRanker()
    cands = [_Cand("A", ev=1.0, confidence=0.4), _Cand("B", ev=1.0, confidence=0.9)]
    ranked = r.rank(cands)
    assert ranked[0].symbol == "B"


def test_combined_spread_and_winrate():
    r = CrossInstrumentRanker(
        spread_pips_lookup=lambda s: 5.0,
        win_rate_lookup=lambda s: 0.6,
        spread_ev_penalty_per_pip=0.02,
        winrate_ev_weight=0.5,
    )
    c = _Cand("EURUSD", ev=1.0)
    # 1.0 - 0.1 + 0.5*(0.1) = 0.95
    assert abs(r.adjusted_ev(c) - 0.95) < 1e-9


def test_negative_ev_sorts_last():
    r = CrossInstrumentRanker()
    cands = [_Cand("A", ev=-0.3), _Cand("B", ev=0.1)]
    ranked = r.rank(cands)
    assert ranked[0].symbol == "B" and ranked[-1].symbol == "A"
