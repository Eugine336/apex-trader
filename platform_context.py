"""
APEX TRADER — PlatformContext
==============================
A dataclass that every module receives at startup and consults before making
any decision that depends on *how* the underlying platform works.

Every trade-management, sizing, and risk call must check this object instead
of hard-coding MT5 assumptions.  The context is constructed once by
PlatformManager and passed into the engine layer — it never mutates at runtime.

Sizing modes
------------
  lots   — standard lot-based sizing (MT5 Forex, Indices, Commodities)
  stake  — dollar-stake sizing (Deriv multiplier / synthetic contracts)

Stop-loss units
---------------
  price  — MT5 accepts absolute price levels
  dollars — Deriv accepts dollar-amount stop-losses

Partial-close support
---------------------
  True  — broker allows partial close of an open order (MT5)
  False — contract is all-or-nothing; simulate via close + re-open (Deriv)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from config import InstrumentCategory


# ---------------------------------------------------------------------------
# PlatformContext
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlatformContext:
    """Immutable per-platform context object.  Build once, share everywhere."""

    # ── Identity ────────────────────────────────────────────────────────
    platform: str           # "mt5" | "deriv"
    broker: str             # "icmarkets" | "exness" | "deriv" | …

    # ── Sizing ──────────────────────────────────────────────────────────
    sizing_mode: str        # "lots" | "stake"

    # ── Order semantics ─────────────────────────────────────────────────
    supports_partial_close: bool   # can close a fraction of an open position
    supports_modify: bool          # can modify SL/TP of an open order
    sl_unit: str                   # "price" | "dollars"

    # ── Spread baseline (broker-specific) ───────────────────────────────
    typical_spreads: dict[str, float] = field(default_factory=dict)
    # symbol → typical spread in pips; used by spread-protection logic

    # ── Contract limits ─────────────────────────────────────────────────
    min_lot: float = 0.01
    max_lot: float = 100.0
    lot_step: float = 0.01

    # ── Deriv-specific ──────────────────────────────────────────────────
    # Maximum payout multiplier for stake calculations
    deriv_max_multiplier: int = 500

    # ── Helpers ─────────────────────────────────────────────────────────

    @property
    def is_mt5(self) -> bool:
        return self.platform == "mt5"

    @property
    def is_deriv(self) -> bool:
        return self.platform == "deriv"

    @property
    def uses_lots(self) -> bool:
        return self.sizing_mode == "lots"

    @property
    def uses_stake(self) -> bool:
        return self.sizing_mode == "stake"

    def typical_spread(self, symbol: str, fallback: float = 2.0) -> float:
        return self.typical_spreads.get(symbol.upper(), fallback)

    def __str__(self) -> str:
        return (
            f"PlatformContext(platform={self.platform}, broker={self.broker}, "
            f"sizing={self.sizing_mode}, partial_close={self.supports_partial_close}, "
            f"sl_unit={self.sl_unit})"
        )


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

def build_context_for_symbol(
    symbol: str,
    broker: str = "",
    typical_spreads: Optional[dict[str, float]] = None,
) -> PlatformContext:
    """
    Build the right PlatformContext for a given symbol by looking it up in
    the instrument registry.  Falls back to MT5/lots if the symbol is unknown.

    Parameters
    ----------
    symbol:           Trading symbol, e.g. "EURUSD", "V75"
    broker:           Human-readable broker identifier (e.g. "icmarkets")
    typical_spreads:  Override per-symbol spread baselines for this broker
    """
    from config import INSTRUMENT_REGISTRY, Platform as Plt

    info = INSTRUMENT_REGISTRY.get(symbol.upper())
    if info is None:
        # Unknown symbol — assume MT5 forex defaults
        return _mt5_context(broker, typical_spreads or {})

    if info.platform in (Plt.DERIV,):
        return _deriv_context(broker, typical_spreads or {})

    if info.platform in (Plt.MT5, Plt.BOTH):
        return _mt5_context(broker, typical_spreads or {})

    # Category fallback
    if info.category == InstrumentCategory.SYNTHETIC:
        return _deriv_context(broker, typical_spreads or {})

    return _mt5_context(broker, typical_spreads or {})


def _mt5_context(broker: str, typical_spreads: dict[str, float]) -> PlatformContext:
    return PlatformContext(
        platform="mt5",
        broker=broker or "mt5",
        sizing_mode="lots",
        supports_partial_close=True,
        supports_modify=True,
        sl_unit="price",
        typical_spreads=typical_spreads,
        min_lot=0.01,
        max_lot=100.0,
        lot_step=0.01,
    )


def _deriv_context(broker: str, typical_spreads: dict[str, float]) -> PlatformContext:
    return PlatformContext(
        platform="deriv",
        broker=broker or "deriv",
        sizing_mode="stake",
        supports_partial_close=False,
        supports_modify=True,       # Deriv multiplier SL/TP can be modified via contract_update
        sl_unit="dollars",
        typical_spreads=typical_spreads,
        min_lot=0.0,
        max_lot=0.0,
        lot_step=0.0,
        deriv_max_multiplier=500,
    )
