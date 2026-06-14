"""
Test for #10 — broker closed-trade history ingest (round-trip grouping).

`group_into_roundtrips` is pure: it pairs broker deals (entry IN + exit OUT) by
position_id into completed trades, sums profit/commission/swap/fee into net P&L,
and skips still-open positions.
"""

from persistence.broker_history import group_into_roundtrips


def _deal(pid, entry, dtype, time, profit=0.0, commission=0.0, swap=0.0,
          symbol="EURUSD", volume=0.1, price=1.10):
    return {
        "position_id": pid, "entry": entry, "type": dtype, "time": time,
        "profit": profit, "commission": commission, "swap": swap, "fee": 0.0,
        "symbol": symbol, "volume": volume, "price": price, "magic": 0,
    }


def test_roundtrip_sums_costs_and_direction():
    deals = [
        _deal(100, entry=0, dtype=0, time=1, price=1.10),                       # IN buy
        _deal(100, entry=1, dtype=1, time=2, price=1.11,
              profit=10.0, commission=-1.0, swap=-0.5),                          # OUT
    ]
    rts = group_into_roundtrips(deals)
    assert len(rts) == 1
    r = rts[0]
    assert r["direction"] == "BUY"
    assert r["open_price"] == 1.10 and r["close_price"] == 1.11
    assert r["gross_profit"] == 10.0
    assert r["net_pnl"] == round(10.0 - 1.0 - 0.5, 2)  # commission + swap netted


def test_open_position_skipped():
    deals = [_deal(200, entry=0, dtype=0, time=1)]  # only an IN deal, no close
    assert group_into_roundtrips(deals) == []


def test_direction_sell_from_entry_deal():
    deals = [
        _deal(300, entry=0, dtype=1, time=1),               # IN sell
        _deal(300, entry=1, dtype=0, time=2, profit=5.0),   # OUT
    ]
    assert group_into_roundtrips(deals)[0]["direction"] == "SELL"


def test_multiple_positions_grouped_separately():
    deals = [
        _deal(1, entry=0, dtype=0, time=1),
        _deal(1, entry=1, dtype=1, time=2, profit=3.0),
        _deal(2, entry=0, dtype=0, time=3),
        _deal(2, entry=1, dtype=1, time=4, profit=-2.0),
    ]
    rts = group_into_roundtrips(deals)
    assert len(rts) == 2
    assert {r["net_pnl"] for r in rts} == {3.0, -2.0}
