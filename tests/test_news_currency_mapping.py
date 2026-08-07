"""
Test for the news-accuracy fix — NewsGuard currency mapping now covers
non-forex instruments (crypto / metals) so USD-driven events (FOMC/CPI) can
freeze e.g. BTCUSD / XAUUSD, not just forex pairs.
"""

from brain.session_engine import NewsGuard


def test_forex_maps_both_legs():
    ng = NewsGuard()
    assert set(ng._get_currencies_from_pairs(["EURUSD"])) == {"EUR", "USD"}


def test_nonforex_maps_to_quote_currency():
    ng = NewsGuard()
    assert set(ng._get_currencies_from_pairs(["BTCUSD"])) == {"USD"}
    assert set(ng._get_currencies_from_pairs(["XAUUSD"])) == {"USD"}


def test_mixed_basket():
    ng = NewsGuard()
    ccy = set(ng._get_currencies_from_pairs(["EURUSD", "BTCUSD", "XAUUSD"]))
    assert ccy == {"EUR", "USD"}


def test_unknown_symbol_contributes_nothing():
    ng = NewsGuard()
    # No trailing known-currency code → no spurious currency.
    assert ng._get_currencies_from_pairs(["US30"]) == []
