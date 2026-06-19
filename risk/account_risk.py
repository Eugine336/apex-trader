"""
APEX TRADER — Per-account risk silos.

Each broker/account (e.g. a $5 Deriv account and a $10 MT5 account) is tracked
**independently**: its own balance, its own daily realised P&L (with a
loss-cap halt), and its own live heat.  A trade on one account is sized and
gated only against THAT account, so a position on the $10 account never
inflates the $5 account's risk picture (and vice-versa).

This sits alongside the portfolio-wide guards (drawdown guard, governor) which
remain as a global backstop — the per-account layer is the primary silo.

Leaf module — standard library + loguru only.
"""

from __future__ import annotations

import functools
import threading

from loguru import logger


def _synchronized(method):
    """Run ``method`` while holding the instance's re-entrant ``_lock``."""
    @functools.wraps(method)
    def _wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return _wrapper


def _lock_public_methods(cls):
    """Wrap every public method so it runs under the instance's ``_lock``.

    Underscore-prefixed methods (including ``__init__``) are skipped: private
    helpers already run under the lock held by their public caller, and skipping
    ``__init__`` guarantees ``_lock`` exists before any wrapped call.
    """
    for name, attr in list(vars(cls).items()):
        if callable(attr) and not name.startswith("_"):
            setattr(cls, name, _synchronized(attr))
    return cls


@_lock_public_methods
class AccountRiskManager:
    """Tracks balance, daily P&L (loss-cap halt) and heat per account key."""

    def __init__(
        self,
        daily_loss_cap_pct: float = 3.0,
        daily_loss_recovery_pct: float = 1.5,
        heat_block_pct: float = 2.0,
        daily_loss_flatten_pct: float = 5.0,
    ) -> None:
        self._lock = threading.RLock()
        self.daily_loss_cap_pct = daily_loss_cap_pct
        self.daily_loss_recovery_pct = daily_loss_recovery_pct
        self.heat_block_pct = heat_block_pct
        self.daily_loss_flatten_pct = daily_loss_flatten_pct
        self._balance: dict[str, float] = {}
        self._daily_pnl: dict[str, float] = {}
        self._unrealized: dict[str, float] = {}
        self._halted: dict[str, bool] = {}
        self._heat: dict[str, float] = {}

    # ── Balance ──────────────────────────────────────────────────────────

    def update_balance(self, account: str, balance: float) -> None:
        # A balance of exactly 0.0 is a VALID, important state (e.g. after a
        # margin call) — not "missing data". Only reject None / negatives.
        if account and balance is not None and balance >= 0:
            self._balance[account] = float(balance)

    def balance(self, account: str) -> float:
        return self._balance.get(account, 0.0)

    def total_balance(self) -> float:
        """Sum of all known per-account balances = total portfolio equity.

        Used as the denominator for the GLOBAL drawdown backstop so the pooled
        daily/weekly P&L percentage reflects the whole portfolio, not whichever
        single account happened to trade last.
        """
        return float(sum(self._balance.values()))

    # ── Heat (live capital-at-risk %, computed per account each cycle) ────

    def set_heat(self, account: str, heat_pct: float) -> None:
        if account:
            self._heat[account] = float(heat_pct)

    def heat(self, account: str) -> float:
        return self._heat.get(account, 0.0)

    def heat_blocked(self, account: str) -> bool:
        return self._heat.get(account, 0.0) >= self.heat_block_pct

    def clear_heat(self) -> None:
        self._heat.clear()

    # ── Daily P&L / loss-cap halt (per account, with hysteresis) ─────────

    def register_realized(self, account: str, pnl_dollars: float) -> None:
        if not account:
            return
        try:
            self._daily_pnl[account] = self._daily_pnl.get(account, 0.0) + float(pnl_dollars)
        except (TypeError, ValueError):
            return
        self._evaluate_halt(account)

    def daily_pnl(self, account: str) -> float:
        return self._daily_pnl.get(account, 0.0)

    def daily_pnl_pct(self, account: str) -> float:
        bal = self._balance.get(account, 0.0)
        if bal <= 0:
            return 0.0
        return self._daily_pnl.get(account, 0.0) / bal * 100.0

    # ── Unrealized (open) P&L — refreshed each cycle from broker truth ───

    def update_unrealized(self, account: str, pnl_dollars: float) -> None:
        """Set the account's current OPEN (unrealized) P&L, then re-evaluate the
        loss-cap halt against realized + unrealized combined. This is what lets
        a deep open loss trip the halt before the trade is ever closed."""
        if not account:
            return
        try:
            self._unrealized[account] = float(pnl_dollars)
        except (TypeError, ValueError):
            return
        self._evaluate_halt(account)

    def unrealized(self, account: str) -> float:
        return self._unrealized.get(account, 0.0)

    def combined_pnl(self, account: str) -> float:
        return self._daily_pnl.get(account, 0.0) + self._unrealized.get(account, 0.0)

    def combined_pnl_pct(self, account: str) -> float:
        bal = self._balance.get(account, 0.0)
        if bal <= 0:
            return 0.0
        return self.combined_pnl(account) / bal * 100.0

    def _evaluate_halt(self, account: str) -> None:
        bal = self._balance.get(account, 0.0)
        if bal <= 0:
            return
        pct = self.combined_pnl_pct(account)
        if self._halted.get(account, False):
            if pct >= -self.daily_loss_recovery_pct:
                self._halted[account] = False
                logger.info(
                    "[AccountRisk] {} daily loss recovered to {:.2f}% (incl. open) — resuming entries",
                    account, pct,
                )
        elif pct <= -self.daily_loss_cap_pct:
            self._halted[account] = True
            logger.warning(
                "[AccountRisk] {} HALTED — daily loss {:.2f}% (incl. open) breached −{:.1f}% cap",
                account, pct, self.daily_loss_cap_pct,
            )

    def daily_loss_halted(self, account: str) -> bool:
        # Re-evaluate against the latest balance before answering.
        self._evaluate_halt(account)
        return self._halted.get(account, False)

    def flatten_breached(self, account: str) -> bool:
        """True if combined (realized + unrealized) loss has breached the harder
        flatten cap — the account should be derisked/flattened immediately."""
        bal = self._balance.get(account, 0.0)
        if bal <= 0:
            return False
        return self.combined_pnl_pct(account) <= -self.daily_loss_flatten_pct

    def reset_daily(self) -> None:
        if any(self._halted.values()):
            logger.info("[AccountRisk] daily reset — per-account loss-cap halts lifted")
        self._daily_pnl.clear()
        self._unrealized.clear()
        self._halted.clear()

    def snapshot(self) -> dict:
        """Per-account view for the dashboard (balance, daily/open P&L, halt)."""
        accounts = (
            set(self._balance) | set(self._daily_pnl) | set(self._unrealized)
            | set(self._heat) | set(self._halted)
        )
        out: dict[str, dict] = {}
        for a in accounts:
            out[a] = {
                "balance": round(self._balance.get(a, 0.0), 2),
                "daily_pnl": round(self._daily_pnl.get(a, 0.0), 2),
                "unrealized": round(self._unrealized.get(a, 0.0), 2),
                "combined_pnl_pct": round(self.combined_pnl_pct(a), 2),
                "heat_pct": round(self._heat.get(a, 0.0), 2),
                "halted": bool(self._halted.get(a, False)),
                "flatten_breached": self.flatten_breached(a),
            }
        return out

    # ── Persistence ──────────────────────────────────────────────────────

    def to_state(self) -> dict:
        return {
            "daily_pnl": dict(self._daily_pnl),
            "halted": dict(self._halted),
        }

    def restore_state(self, state: dict) -> None:
        try:
            self._daily_pnl = {
                str(k): float(v) for k, v in (state.get("daily_pnl") or {}).items()
            }
            self._halted = {
                str(k): bool(v) for k, v in (state.get("halted") or {}).items()
            }
        except (TypeError, ValueError) as exc:
            logger.warning(
                "[AccountRisk] corrupt persisted state ignored — per-account daily "
                "loss caps start FRESH this session: {}", exc,
            )
