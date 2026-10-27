"""Tests for spread bootstrap symbol scoping."""


from types import SimpleNamespace

import config as cfg
from risk.spread_bootstrap import bootstrap_spreads


def test_bootstrap_samples_only_requested_symbols(monkeypatch):
    # Keep a tiny isolated registry so the test never touches runtime state or a broker.
    xau = cfg.INSTRUMENT_REGISTRY["XAUUSD"]
    eur = cfg.INSTRUMENT_REGISTRY["EURUSD"]
    monkeypatch.setattr(cfg, "INSTRUMENT_REGISTRY", {"XAUUSD": xau, "EURUSD": eur})

    class FakePlatformManager:
        def __init__(self):
            self.requested = []

        def get_price(self, symbol):
            self.requested.append(symbol)
            return SimpleNamespace(spread=1.2)

    pm = FakePlatformManager()
    bootstrap_spreads(platform_manager=pm, sample_retries=1, symbols=["XAUUSD"])

    assert pm.requested == ["XAUUSD"]
    assert cfg.INSTRUMENT_REGISTRY["XAUUSD"].typical_spread_pips == 1.2
    assert cfg.INSTRUMENT_REGISTRY["EURUSD"].typical_spread_pips == eur.typical_spread_pips


def test_bootstrap_deduplicates_and_normalizes_requested_symbols(monkeypatch):
    xau = cfg.INSTRUMENT_REGISTRY["XAUUSD"]
    monkeypatch.setattr(cfg, "INSTRUMENT_REGISTRY", {"XAUUSD": xau})

    class FakePlatformManager:
        def __init__(self):
            self.requested = []

        def get_price(self, symbol):
            self.requested.append(symbol)
            return SimpleNamespace(spread=1.3)

    pm = FakePlatformManager()
    bootstrap_spreads(
        platform_manager=pm, sample_retries=1,
        symbols=["xauusd", " XAUUSD ", "XAUUSD"],
    )

    assert pm.requested == ["XAUUSD"]
