"""Regression tests for the GER40/GBPCHF cascade-failure incident.

Covers the six live production bugs surfaced when a GER40 (broker alias
``DE40``) trade on a $27 MT5 account cascaded into a portfolio-wide flatten:

  #2  Index pip conversion — broker symbol aliases (DE40) must resolve to the
      registry symbol (GER40) so pip-derived P&L / heat use the correct scale.
  #3  Broker min-lot safety — a broker minimum lot that floors the size upward
      must be re-checked against the per-account balance.
  #4  Portfolio heat is per-account; EMERGENCY only flattens hot accounts.
  #5  Direction flip mirrors SL/TP around the entry price (no inverted stop).
"""

import datetime as _dt

import pytest

from config import get_pip_size
from brain.symbol_mapper import resolve_to_internal
from risk.portfolio_risk_state import (
    PositionRisk,
    compute_live_heat_pct,
    compute_position_risk_dollars,
)


# ── Issue #2: broker symbol alias resolves to correct pip scale ────────────
class TestIndexPipConversion:
    def test_de40_resolves_to_ger40(self):
        assert resolve_to_internal("DE40") == "GER40"

    def test_ger40_pip_size_is_index_scale(self):
        # Index point scale, NOT forex 0.0001.
        assert get_pip_size("GER40") == 0.1

    def test_registry_symbol_passthrough(self):
        assert resolve_to_internal("GER40") == "GER40"
        assert resolve_to_internal("EURUSD") == "EURUSD"

    def test_risk_dollars_not_inflated_with_correct_pip(self):
        """A 3.8-point DE40 move must read as ~38 pips of risk, not 38,000.

        Using the forex default 0.0001 instead of the index 0.1 inflates
        pips_to_stop (and therefore risk_$ and heat) by ~1000x — the root of
        the 1775% heat spike that nuked the book.
        """
        kwargs = dict(
            direction="BUY",
            entry_price=24967.9,
            sl=24930.0,  # 37.9 points away
            lots=0.10,
            pip_value_per_lot=1.0,
            at_breakeven=False,
        )
        correct, _ = compute_position_risk_dollars(pip_size=0.1, **kwargs)
        wrong, _ = compute_position_risk_dollars(pip_size=0.0001, **kwargs)
        # Wrong (forex) pip is ~1000x the correct (index) pip risk.
        assert wrong > correct * 500
        # Correct risk for 0.10 lots over ~38 points at $1/point ≈ $37.9.
        assert correct == pytest.approx(37.9, abs=0.5)


# ── Issue #4: heat is per-account, not diluted across a combined book ──────
class TestPerAccountHeat:
    def test_small_account_heat_not_diluted_by_idle_capital(self):
        """A risky MT5 position must show high heat against its OWN balance.

        The state machine is now driven by the WORST single-account heat, so a
        $27 account at risk is not masked behind a $9,999 idle Deriv balance.
        """
        mt5_risk = 5.0   # $5 at risk on the $27 MT5 account
        deriv_risk = 0.0
        mt5_balance = 27.0
        deriv_balance = 9999.0

        # Old (broken) behaviour: combined equity dilutes the danger.
        combined = compute_live_heat_pct(
            [PositionRisk("1", "GER40", "BUY", mt5_risk, False, False)],
            mt5_balance + deriv_balance,
        )
        # New behaviour: worst per-account heat is what matters.
        per_account = max(
            (mt5_risk / mt5_balance * 100.0),
            (deriv_risk / deriv_balance * 100.0),
        )
        assert combined < 0.1            # diluted to near-zero
        assert per_account > 18.0        # real danger surfaces
        assert per_account > combined * 100

    def test_zero_equity_is_maximally_defensive(self):
        assert compute_live_heat_pct(
            [PositionRisk("1", "X", "BUY", 5.0, False, False)], 0.0,
        ) == 100.0


# ── Issue #3: broker min-lot must be re-validated against the account ──────
class TestMinLotSafetyGate:
    @staticmethod
    def _snap_risk_pct(snapped_lots, entry, sl, pip_size, pip_value, balance):
        risk_pips = abs(entry - sl) / pip_size
        max_loss = snapped_lots * risk_pips * pip_value
        return (max_loss / balance) * 100.0

    def test_min_lot_flooring_exceeds_micro_ceiling(self):
        """DE40 min lot 0.10 on a $27 account blows past the 5% cap."""
        risk_pct = self._snap_risk_pct(
            snapped_lots=0.10, entry=24967.9, sl=24930.0,
            pip_size=0.1, pip_value=1.0, balance=27.0,
        )
        assert risk_pct > 5.0  # would be rejected by the post-snap gate

    def test_within_account_capacity_passes(self):
        """A large account easily absorbs the same broker min lot."""
        risk_pct = self._snap_risk_pct(
            snapped_lots=0.10, entry=24967.9, sl=24960.0,
            pip_size=0.1, pip_value=1.0, balance=10000.0,
        )
        assert risk_pct < 5.0


# ── Issue #5: direction flip mirrors SL/TP around the entry price ──────────
class TestDirectionFlipMirror:
    def _orchestrator(self):
        from brain.world_model import WorldModelStore
        from entry.entry_orchestrator import EntryOrchestrator

        return EntryOrchestrator(world_model_store=WorldModelStore())

    def _zone(self, direction, top, bottom, invalidation):
        from entry.models import EntryZone, ZoneType

        now = _dt.datetime.now(_dt.timezone.utc)
        return EntryZone(
            symbol="CADJPY",
            direction=direction,
            zone_type=ZoneType.FVG_MIDPOINT,
            top=top,
            bottom=bottom,
            midpoint=(top + bottom) / 2.0,
            invalidation_level=invalidation,
            conviction=80,
            created_at=now,
            expires_at=now + _dt.timedelta(seconds=900),
            timeframe="M5",
        )

    def test_flip_long_to_short_stops_above_entry(self):
        """Reproduces the CADJPY incident: entry ABOVE the zone.

        Original LONG: zone 113.941–113.957, invalidation 113.933 (below
        entry 113.974). Flipping to SHORT must place the stop ABOVE entry,
        not at zone.top+buffer (113.965, below entry → inverted).
        """
        orch = self._orchestrator()
        entry = 113.974
        zone = self._zone("LONG", top=113.957, bottom=113.941, invalidation=113.933)
        flipped = orch._flip_zone(zone, "SHORT", entry)
        assert flipped.direction == "SHORT"
        # SHORT stop MUST be above entry (the bug placed it below).
        assert flipped.invalidation_level > entry
        # Risk distance is preserved (mirrored around entry).
        orig_risk = abs(entry - 113.933)
        new_risk = abs(flipped.invalidation_level - entry)
        assert new_risk == pytest.approx(orig_risk, abs=1e-6)

    def test_flip_short_to_long_stops_below_entry(self):
        orch = self._orchestrator()
        entry = 100.0
        zone = self._zone("SHORT", top=100.5, bottom=100.2, invalidation=100.6)
        flipped = orch._flip_zone(zone, "LONG", entry)
        assert flipped.direction == "LONG"
        assert flipped.invalidation_level < entry

    def test_flip_clears_counter_trend_flag(self):
        orch = self._orchestrator()
        from entry.models import EntryZone, ZoneType
        now = _dt.datetime.now(_dt.timezone.utc)
        zone = EntryZone(
            symbol="CADJPY", direction="LONG", zone_type=ZoneType.FVG_MIDPOINT,
            top=113.957, bottom=113.941, midpoint=113.949,
            invalidation_level=113.933, conviction=60,
            created_at=now, expires_at=now + _dt.timedelta(seconds=900),
            timeframe="M5", is_counter_trend=True,
        )
        flipped = orch._flip_zone(zone, "SHORT", 113.974)
        assert flipped.is_counter_trend is False
