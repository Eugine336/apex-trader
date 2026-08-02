"""Tests for Phase 2 Feature D — DXY (USD-strength) correlation filter.

The filter is a conviction PENALTY (never a block): when USD strength opposes a
USD-denominated trade (USD strengthening while LONG gold, or weakening while
SHORT gold) the orchestrator haircuts conviction by dxy_opposition_penalty. The
USD reading is provided by an injected callable that receives the configurable
lookback window.
"""

from types import SimpleNamespace

import entry.entry_orchestrator as orch_mod
from brain.world_model import WorldModelStore
from entry.entry_orchestrator import EntryOrchestrator
from entry.models import EntryConfig


def _analysis(usd_momentum="RISING", usd_score=0.3):
    return SimpleNamespace(rankings=[
        SimpleNamespace(currency="USD", momentum=usd_momentum, score=usd_score),
        SimpleNamespace(currency="EUR", momentum="FALLING", score=-0.2),
        SimpleNamespace(currency="XAU", momentum="FALLING", score=-0.1),
    ])


def _orch(reader, config=None):
    return EntryOrchestrator(
        world_model_store=WorldModelStore(),
        config=config or EntryConfig(),
        pip_size_lookup=lambda _s: 0.0001,
        get_currency_strength=reader,
    )


class TestDxyConfig:
    def test_entry_config_defaults(self):
        c = EntryConfig()
        assert c.dxy_filter_enabled is True
        assert c.dxy_opposition_penalty == 0.15
        assert c.dxy_lookback_bars == 20

    def test_gold_profile_values(self):
        from brain.instrument_profile import get_profile
        g = get_profile("XAUUSD")
        assert g.dxy_filter_enabled is True
        assert g.dxy_opposition_penalty == 0.15
        assert g.dxy_lookback_bars == 20


class TestDxyOpposition:
    def test_usd_rising_opposes_long_gold(self):
        orch = _orch(lambda s, lb: _analysis("RISING"))
        assert orch._dxy_opposition_penalty("XAUUSD", "LONG") == 0.15

    def test_usd_rising_does_not_oppose_short_gold(self):
        orch = _orch(lambda s, lb: _analysis("RISING"))
        assert orch._dxy_opposition_penalty("XAUUSD", "SHORT") == 0.0

    def test_usd_falling_opposes_short_gold(self):
        orch = _orch(lambda s, lb: _analysis("FALLING"))
        assert orch._dxy_opposition_penalty("XAUUSD", "SHORT") == 0.15

    def test_usd_falling_does_not_oppose_long_gold(self):
        orch = _orch(lambda s, lb: _analysis("FALLING"))
        assert orch._dxy_opposition_penalty("XAUUSD", "LONG") == 0.0

    def test_stable_uses_score_sign(self):
        # STABLE momentum → strengthening decided by score sign (positive here).
        orch = _orch(lambda s, lb: _analysis("STABLE", usd_score=0.4))
        assert orch._dxy_opposition_penalty("XAUUSD", "LONG") == 0.15
        assert orch._dxy_opposition_penalty("XAUUSD", "SHORT") == 0.0

    def test_non_usd_symbol_no_penalty(self):
        orch = _orch(lambda s, lb: _analysis("RISING"))
        assert orch._dxy_opposition_penalty("EURGBP", "LONG") == 0.0

    def test_no_reader_no_penalty(self):
        orch = _orch(None)
        assert orch._dxy_opposition_penalty("XAUUSD", "LONG") == 0.0

    def test_reader_returns_none_no_penalty(self):
        orch = _orch(lambda s, lb: None)
        assert orch._dxy_opposition_penalty("XAUUSD", "LONG") == 0.0

    def test_disabled_no_penalty(self, monkeypatch):
        monkeypatch.setattr(orch_mod, "get_profile", lambda _s: None)
        orch = _orch(
            lambda s, lb: _analysis("RISING"),
            config=EntryConfig(dxy_filter_enabled=False),
        )
        assert orch._dxy_opposition_penalty("XAUUSD", "LONG") == 0.0

    def test_lookback_passed_to_reader(self):
        calls = []

        def reader(symbol, lookback):
            calls.append((symbol, lookback))
            return _analysis("RISING")

        orch = _orch(reader)
        orch._dxy_opposition_penalty("XAUUSD", "LONG")
        assert calls == [("XAUUSD", 20)]  # gold profile dxy_lookback_bars


class TestDxyConvictionHaircut:
    def test_penalty_reduces_conviction_not_block(self):
        orch = _orch(lambda s, lb: _analysis("RISING"))
        shaped = orch._shaped_conviction("XAUUSD", "LONG", 100, "RANGING", "NY")
        # 100 * (1 - 0.15) = 85 — reduced, NOT blocked (not None).
        assert shaped == 85
        assert orch.stats.get("dxy_oppositions", 0) == 1

    def test_no_penalty_when_aligned(self):
        orch = _orch(lambda s, lb: _analysis("RISING"))
        # USD rising + SHORT gold → aligned, no haircut.
        shaped = orch._shaped_conviction("XAUUSD", "SHORT", 100, "RANGING", "NY")
        assert shaped == 100
