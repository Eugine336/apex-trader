"""APEX TRADER — Position Displacer tests (GAP 4).

Covers: disabled = no-op, EV-margin gate, winner-protect floor, weakest-position
selection, cooldown, max-per-cycle cap, empty book, and stats.
"""

from management.position_displacer import (
    DisplacementDecision,
    PositionDisplacer,
    PositionEV,
)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_disabled_never_displaces():
    d = PositionDisplacer(enabled=False)
    out = d.evaluate(5.0, [PositionEV("1", "EURUSD", "LONG", ev=0.0, profit_r=0.0)])
    assert out.displace is False
    assert out.reason == "disabled"


def test_empty_book_no_displace():
    d = PositionDisplacer(enabled=True)
    out = d.evaluate(5.0, [])
    assert out.displace is False


def test_displaces_weaker_position():
    d = PositionDisplacer(enabled=True, ev_margin=0.5)
    book = [
        PositionEV("1", "EURUSD", "LONG", ev=0.2, profit_r=0.0),
        PositionEV("2", "GBPJPY", "SHORT", ev=1.0, profit_r=0.0),
    ]
    out = d.evaluate(1.0, book, candidate_symbol="XAUUSD")
    assert out.displace is True
    assert out.target.ticket == "1"  # weakest EV


def test_insufficient_margin_blocks():
    d = PositionDisplacer(enabled=True, ev_margin=0.5)
    book = [PositionEV("1", "EURUSD", "LONG", ev=0.8, profit_r=0.0)]
    out = d.evaluate(1.0, book)  # margin 0.2 < 0.5
    assert out.displace is False
    assert "insufficient" in out.reason.lower()


def test_winner_is_protected():
    d = PositionDisplacer(enabled=True, ev_margin=0.5, min_profit_protect=1.0)
    # The only position is a +1.5R winner → protected → not eligible.
    book = [PositionEV("1", "EURUSD", "LONG", ev=0.1, profit_r=1.5)]
    out = d.evaluate(5.0, book)
    assert out.displace is False
    assert "protected" in out.reason.lower() or "no displaceable" in out.reason.lower()


def test_picks_weakest_among_eligible_only():
    d = PositionDisplacer(enabled=True, ev_margin=0.1, min_profit_protect=1.0)
    book = [
        PositionEV("win", "EURUSD", "LONG", ev=-5.0, profit_r=2.0),   # protected
        PositionEV("weak", "GBPJPY", "SHORT", ev=0.3, profit_r=0.0),  # eligible
    ]
    out = d.evaluate(1.0, book)
    assert out.displace is True
    assert out.target.ticket == "weak"


def test_cooldown_blocks_second():
    clk = _Clock()
    d = PositionDisplacer(enabled=True, ev_margin=0.1, cooldown_seconds=300.0, clock=clk)
    book = [PositionEV("1", "EURUSD", "LONG", ev=0.0, profit_r=0.0)]
    out1 = d.evaluate(1.0, book)
    assert out1.displace is True
    d.record_displacement()
    out2 = d.evaluate(1.0, book)
    assert out2.displace is False
    assert "cooldown" in out2.reason.lower()


def test_cooldown_elapsed_allows_again():
    clk = _Clock()
    d = PositionDisplacer(
        enabled=True, ev_margin=0.1, cooldown_seconds=300.0,
        max_per_cycle=5, clock=clk,
    )
    book = [PositionEV("1", "EURUSD", "LONG", ev=0.0, profit_r=0.0)]
    assert d.evaluate(1.0, book).displace is True
    d.record_displacement()
    clk.advance(301.0)
    assert d.evaluate(1.0, book).displace is True


def test_max_per_cycle_cap():
    clk = _Clock()
    d = PositionDisplacer(
        enabled=True, ev_margin=0.1, max_per_cycle=1,
        cooldown_seconds=0.0, clock=clk,
    )
    book = [PositionEV("1", "EURUSD", "LONG", ev=0.0, profit_r=0.0)]
    assert d.evaluate(1.0, book).displace is True
    d.record_displacement()
    # cooldown 0 so not cooldown-blocked, but the per-cycle cap is reached.
    out = d.evaluate(1.0, book)
    assert out.displace is False
    assert "cycle" in out.reason.lower()


def test_reset_cycle_allows_again():
    clk = _Clock()
    d = PositionDisplacer(
        enabled=True, ev_margin=0.1, max_per_cycle=1,
        cooldown_seconds=0.0, clock=clk,
    )
    book = [PositionEV("1", "EURUSD", "LONG", ev=0.0, profit_r=0.0)]
    assert d.evaluate(1.0, book).displace is True
    d.record_displacement()
    assert d.evaluate(1.0, book).displace is False
    d.reset_cycle()
    assert d.evaluate(1.0, book).displace is True


def test_stats_tracks_total():
    d = PositionDisplacer(enabled=True, ev_margin=0.1, cooldown_seconds=0.0)
    book = [PositionEV("1", "EURUSD", "LONG", ev=0.0, profit_r=0.0)]
    d.evaluate(1.0, book)
    d.record_displacement()
    st = d.stats()
    assert st["enabled"] is True
    assert st["total_displacements"] == 1
