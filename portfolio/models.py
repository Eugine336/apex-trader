"""Input models for the Portfolio Division.

These are plain data carriers. The Portfolio Division consumes them and never
reaches back into the bootstrap, the broker layer, or any subsystem — every
value it needs to size a trade and judge portfolio fit is passed in explicitly
so the division stays decoupled and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence


@dataclass
class PortfolioCandidate:
    """A single trade the organisation wants to size.

    Direction/conviction come from Consensus; the trade has already passed
    Compliance by the time it reaches Portfolio.
    """

    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    conviction: float = 0.0          # 0..100 (consensus score)
    context: Any = None              # PlatformContext (duck-typed: .uses_stake)
    pip_size: float = 0.0
    pip_value_per_lot: float = 10.0
    broker: str = ""                 # broker/platform slug for exposure budgets


@dataclass
class PortfolioAccount:
    """The account this trade will be sized against (per-account silo)."""

    balance: float
    account_key: str = ""
    # Realised daily P&L for this account (negative for a loss). Used to derive
    # the remaining daily-loss budget. ``daily_loss_cap_pct`` is the per-account
    # halt level (percent of balance).
    daily_pnl: float = 0.0
    daily_loss_cap_pct: float = 0.0


@dataclass
class SizingFactors:
    """The transparent multiplier bundle the organisation has produced.

    Every factor is a pure de-risking scalar in (0, ~2]; the division clamps the
    product to ``[size_floor, 1.0]`` so sizing can only ever reduce risk below
    the per-trade ceiling — never inflate it. ``base_risk_pct`` is the
    (drawdown-adjusted) per-trade risk fraction the sizer starts from.
    """

    base_risk_pct: float = 0.0
    de_size_mult: float = 1.0        # Decision Engine conviction scaling
    orch_mult: float = 1.0           # Orchestrator evidence fold
    vol_mult: float = 1.0            # System-wide volatility monitor
    inst_vol_mult: float = 1.0       # Per-instrument ATR regime
    density_mult: float = 1.0        # Opportunity density
    exec_mult: float = 1.0           # Execution-quality monitor
    cap_mult: float = 1.0            # Capital allocator (strategy allocation)
    adapt_mult: float = 1.0          # Adaptive optimizer learned edge

    def as_ordered(self) -> Sequence[tuple[str, float]]:
        """Return the factors in a stable, log-friendly order."""
        return (
            ("DE", self.de_size_mult),
            ("ORCH", self.orch_mult),
            ("VOL", self.vol_mult),
            ("IVOL", self.inst_vol_mult),
            ("DEN", self.density_mult),
            ("EXEC", self.exec_mult),
            ("CAP", self.cap_mult),
            ("ADAPT", self.adapt_mult),
        )

    def product(self) -> float:
        p = 1.0
        for _name, value in self.as_ordered():
            try:
                p *= float(value)
            except (TypeError, ValueError):
                continue
        return p


@dataclass
class OpenPositionView:
    """A normalised view of an open position for exposure accounting."""

    symbol: str
    direction: str
    broker: str = ""
    risk_pct: float = 0.0


def normalize_book(book: Optional[Sequence[Any]]) -> list[OpenPositionView]:
    """Coerce a heterogeneous list of open positions into ``OpenPositionView``.

    Accepts dataclasses/objects (``.symbol``/``.direction``/``.broker``) or
    dicts. Never raises — an unparseable entry is skipped.
    """
    out: list[OpenPositionView] = []
    for pos in book or []:
        try:
            if isinstance(pos, dict):
                sym = str(pos.get("symbol") or pos.get("pair") or "")
                direction = str(pos.get("direction") or "")
                broker = str(pos.get("broker") or pos.get("platform") or "")
                risk = float(pos.get("risk_pct", 0.0) or 0.0)
            else:
                sym = str(
                    getattr(pos, "symbol", "") or getattr(pos, "pair", "") or ""
                )
                direction = str(getattr(pos, "direction", "") or "")
                broker = str(
                    getattr(pos, "broker", "") or getattr(pos, "platform", "") or ""
                )
                risk = float(getattr(pos, "risk_pct", 0.0) or 0.0)
            if not sym:
                continue
            out.append(
                OpenPositionView(
                    symbol=sym.upper(),
                    direction=direction.upper(),
                    broker=broker,
                    risk_pct=risk,
                )
            )
        except Exception:  # noqa: BLE001 — never let book parsing break sizing
            continue
    return out
