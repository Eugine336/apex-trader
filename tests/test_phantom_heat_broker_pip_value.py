"""Regression tests for the crypto phantom-heat / phantom-EMERGENCY incident.

An XRPUSD position sized at the broker minimum (100 lots) read as ~900% heat
because the heat monitor used the config registry ``pip_value_per_lot`` (a 1.0
forex-scale placeholder) while the sizing path used broker truth
(~0.0001/lot). The two views disagreeing on money-per-pip tripped an EMERGENCY
force-close within seconds of every crypto open.

Covers:
  A/B  the heat monitor sources pip value through the SAME broker-truth helper
       as the sizing path (``_effective_pip_value`` → ``_broker_pip_value_from_spec``)
  B    the centralised helper returns broker truth when available and the
       registry fallback when not
  C    crypto symbols flow into broker auto-discovery and constraint discovery
       captures the tick value/size the pip-value override is derived from
  D    saner crypto registry defaults + a large-lot crypto position no longer
       trips phantom EMERGENCY once broker-truth pip value is used
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from config import get_instrument
from risk.portfolio_risk_state import (
    PositionRisk,
    compute_live_heat_pct,
    compute_position_risk_dollars,
)
from event_driven_bootstrap import (
    EventDrivenSystem,
    PositionEvaluator,
    _broker_pip_value_from_spec,
)


def _spec(tick_value, tick_size):
    return {"trade_tick_value": tick_value, "trade_tick_size": tick_size}


# ── The single shared derivation (_broker_pip_value_from_spec) ──────────────
class TestBrokerPipValueFromSpec:
    def test_derives_from_broker_spec(self):
        # tick_value 0.5 with tick_size == pip_size → 0.5 money/pip.
        pv = _broker_pip_value_from_spec(_spec(0.5, 0.0001), pip_size=0.0001, fallback=1.0)
        assert pv == pytest.approx(0.5)

    def test_scales_by_pip_to_tick_ratio(self):
        # pip_size 10x tick_size → 10x the tick value.
        pv = _broker_pip_value_from_spec(_spec(1.0, 0.01), pip_size=0.1, fallback=99.0)
        assert pv == pytest.approx(10.0)

    def test_falls_back_when_spec_empty(self):
        assert _broker_pip_value_from_spec({}, pip_size=0.0001, fallback=0.0001) == 0.0001

    def test_falls_back_when_spec_incomplete(self):
        # Only tick_value present (no tick_size) → cannot derive → fallback.
        assert _broker_pip_value_from_spec({"trade_tick_value": 1.0}, 0.0001, 7.0) == 7.0

    def test_falls_back_on_zero_tick_size(self):
        assert _broker_pip_value_from_spec(_spec(1.0, 0.0), 0.0001, 3.0) == 3.0


# ── EventDrivenSystem._effective_pip_value (heat monitor + sizing path) ─────
class TestEffectivePipValueEventDrivenSystem:
    def _sys(self, spec):
        sys = EventDrivenSystem.__new__(EventDrivenSystem)
        sys._symbol_spec_cache = {}
        conn = MagicMock()
        conn.get_symbol_spec.return_value = spec
        sys._pm = MagicMock()
        sys._pm.get_connector.return_value = conn
        return sys

    def test_returns_broker_truth_when_available(self):
        sys = self._sys(_spec(0.5, 0.0001))
        # Registry XRPUSD fallback is ~0.0001; broker truth here is 0.5.
        assert sys._effective_pip_value("XRPUSD", 0.0001) == pytest.approx(0.5)

    def test_returns_registry_fallback_when_spec_unavailable(self):
        sys = self._sys({})
        registry = get_instrument("XRPUSD").pip_value_per_lot
        assert sys._effective_pip_value("XRPUSD", 0.0001) == pytest.approx(registry)

    def test_heat_uses_broker_pip_value_not_registry(self):
        """The heat monitor pip value MUST come from broker truth, not registry.

        Mock ``_broker_pip_value`` to a value distinct from the registry and
        confirm ``_effective_pip_value`` (the exact call the heat loop makes)
        returns it, and that the registry value was passed only as the fallback.
        """
        sys = EventDrivenSystem.__new__(EventDrivenSystem)
        sys._broker_pip_value = MagicMock(return_value=0.5)
        pv = sys._effective_pip_value("XRPUSD", 0.0001)
        assert pv == 0.5
        sys._broker_pip_value.assert_called_once()
        args = sys._broker_pip_value.call_args.args
        assert args[0] == "XRPUSD"
        # Fallback passed in is the registry value — broker truth overrides it.
        assert args[2] == pytest.approx(get_instrument("XRPUSD").pip_value_per_lot)


# ── PositionEvaluator._effective_pip_value (scale-in sizing path) ───────────
class TestEffectivePipValueScaleIn:
    def _ev(self, spec):
        ev = PositionEvaluator.__new__(PositionEvaluator)
        ev._pip_size_fallback_symbols = set()
        ev._pm = MagicMock()
        ev._pm.get_symbol_spec.return_value = spec
        return ev

    def test_uses_broker_truth_via_platform_manager(self):
        ev = self._ev(_spec(0.5, 0.0001))
        assert ev._effective_pip_value("XRPUSD", 0.0001) == pytest.approx(0.5)

    def test_registry_fallback_when_pm_spec_empty(self):
        ev = self._ev({})
        registry = get_instrument("XRPUSD").pip_value_per_lot
        assert ev._effective_pip_value("XRPUSD", 0.0001) == pytest.approx(registry)


# ── Phantom EMERGENCY prevention (heat math with broker vs registry pip) ────
class TestCryptoNoPhantomEmergency:
    def test_registry_placeholder_would_trip_phantom_emergency(self):
        # 100-lot XRPUSD (broker minimum), 5-cent stop = 500 pips at 0.0001.
        common = dict(
            direction="LONG", entry_price=2.0, sl=1.95, lots=100.0,
            pip_size=0.0001, at_breakeven=False,
        )
        phantom, _ = compute_position_risk_dollars(pip_value_per_lot=1.0, **common)
        broker, _ = compute_position_risk_dollars(pip_value_per_lot=0.0001, **common)

        equity = 1000.0
        phantom_heat = compute_live_heat_pct(
            [PositionRisk("x", "XRPUSD", "LONG", phantom, False, False)], equity,
        )
        broker_heat = compute_live_heat_pct(
            [PositionRisk("x", "XRPUSD", "LONG", broker, False, False)], equity,
        )

        # Registry placeholder → absurd heat that force-closes the trade.
        assert phantom_heat > 100.0
        # Broker truth → sane real risk, well below any defensive threshold.
        assert broker_heat < 1.0
        assert phantom > broker * 1000

    def test_crypto_registry_defaults_are_order_of_magnitude_sane(self):
        # The 1.0 forex-scale placeholder is gone; sub-$10 coins default to
        # ≈ pip_size (contract_size ≈ 1), the correct order of magnitude.
        assert get_instrument("XRPUSD").pip_value_per_lot < 0.01
        assert get_instrument("ADAUSD").pip_value_per_lot < 0.01
        assert get_instrument("XRPUSD").pip_value_per_lot == pytest.approx(
            get_instrument("XRPUSD").pip_size,
        )


# ── Broker auto-discovery now covers crypto ─────────────────────────────────
class TestCryptoAutodiscovery:
    CRYPTO = ("BTCUSD", "ETHUSD", "LTCUSD", "XRPUSD", "BNBUSD", "SOLUSD", "ADAUSD", "DOTUSD")

    def test_crypto_in_apex_aliases(self):
        from brain.broker_autodiscovery import APEX_ALIASES

        for sym in self.CRYPTO:
            assert sym in APEX_ALIASES, f"{sym} missing from APEX_ALIASES"

    def test_match_all_maps_crypto_symbols(self):
        from brain.broker_autodiscovery import BrokerAutoDiscovery

        disc = BrokerAutoDiscovery("test_broker")
        overrides = disc._match_all(["XRPUSD", "BTCUSD", "EURUSD"])
        assert overrides.get("XRPUSD") == "XRPUSD"
        assert overrides.get("BTCUSD") == "BTCUSD"

    def test_constraint_discovery_captures_tick_value_and_size(self):
        import platforms.mt5.mt5_discovery as disc

        fake_info = SimpleNamespace(
            volume_min=0.01, volume_max=100.0, volume_step=0.01,
            trade_stops_level=0, digits=5, point=0.0001,
            trade_contract_size=1.0, trade_tick_value=0.0001, trade_tick_size=0.0001,
        )
        fake_mt5 = MagicMock()
        fake_mt5.initialize.return_value = True
        fake_mt5.symbol_info.return_value = fake_info

        with patch.object(disc, "mt5", fake_mt5), patch.object(disc, "_MT5_AVAILABLE", True):
            out = disc.discover_symbol_constraints(["XRPUSD"])

        assert "XRPUSD" in out
        assert out["XRPUSD"]["trade_tick_value"] == pytest.approx(0.0001)
        assert out["XRPUSD"]["trade_tick_size"] == pytest.approx(0.0001)
        assert out["XRPUSD"]["contract_size"] == pytest.approx(1.0)
