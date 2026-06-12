"""
APEX TRADER — Portfolio Governor.

Per-trade risk lives in `risk.risk_engine`.  The Governor adds the missing
*portfolio-level* layer: it stops the system opening too many correlated
positions, over-concentrating in one currency or sector, exceeding a hard
position cap, or trading on after a bad day.

Design rules:
  * Advisory to the planner — it returns a verdict, it is NOT a hard gate in
    the execution layer.  Defence in depth, not a single point of failure.
  * Fail-open — if the governor code raises, the trade is ALLOWED.  We never
    halt a live trading system because the safety advisor crashed.
  * Every block is logged at INFO (these are important safety events) and
    retained in a short ring buffer for the dashboard.
  * All thresholds come from `GovernorConfig` — no magic numbers.

Leaf-ish module — imports `config` lazily inside methods to avoid an
import cycle (config imports GovernorConfig from governor.models).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from governor.models import GovernorConfig, GovernorVerdict

# Currencies we can confidently parse out of a symbol's two legs.
_KNOWN_CURRENCIES = frozenset(
    {
        "USD",
        "EUR",
        "GBP",
        "JPY",
        "AUD",
        "NZD",
        "CAD",
        "CHF",
        "XAU",
        "XAG",
        "XTI",
        "XBR",  # metals / oil legs
        "BTC",
        "ETH",
        "ADA",
        "DOT",
        "SOL",
        "XRP",
        "BNB",
        "LTC",  # crypto bases
    }
)


class PortfolioGovernor:
    """Portfolio-level risk authority — advisory to the planner."""

    def __init__(self, config: GovernorConfig | None = None) -> None:
        self.config = config or GovernorConfig()
        self.daily_pnl: float = 0.0
        self.daily_trading_halted: bool = False
        self._reference_balance: float = 0.0
        self._recent_blocks: deque[dict] = deque(maxlen=50)

    # ── Main entry point ─────────────────────────────────────────────────

    def check(
        self,
        symbol: str,
        direction: str,
        open_positions: list[Any] | None = None,
        account_balance: float = 0.0,
    ) -> GovernorVerdict:
        """Decide whether the portfolio can take ``symbol`` in ``direction``.

        Fail-open: any internal error returns an *allowed* verdict so the
        governor can never halt a live system by crashing.
        """
        try:
            return self._check_inner(symbol, direction, open_positions or [], account_balance)
        except Exception as exc:  # noqa: BLE001 — fail-open is intentional
            logger.warning(
                "[Governor] check error for {} {} — failing OPEN (allowing): {}",
                direction,
                symbol,
                exc,
            )
            return GovernorVerdict(allowed=True, reason=f"governor error (fail-open): {exc}")

    def _check_inner(
        self,
        symbol: str,
        direction: str,
        open_positions: list[Any],
        account_balance: float,
    ) -> GovernorVerdict:
        cfg = self.config
        if not cfg.enabled:
            return GovernorVerdict(allowed=True, reason="governor disabled")

        if account_balance and account_balance > 0:
            self._reference_balance = account_balance
            self._evaluate_halt()

        # ── Check 1: daily loss cap ──────────────────────────────────────
        if self.daily_trading_halted:
            pct = self._daily_pnl_pct()
            return self._block(
                symbol,
                direction,
                "daily_loss_cap",
                f"daily loss cap hit ({pct:.2f}% ≤ −{cfg.daily_loss_cap_pct:.1f}%) — "
                f"trading halted until recovery to −{cfg.daily_loss_recovery_pct:.1f}%",
            )

        positions = [self._normalize(p) for p in open_positions]
        positions = [p for p in positions if p is not None]

        # ── Check 2: max open positions ──────────────────────────────────
        if len(positions) >= cfg.max_open_positions:
            return self._block(
                symbol,
                direction,
                "max_positions",
                f"max open positions reached ({len(positions)}/{cfg.max_open_positions})",
            )

        prop_legs = self._signed_legs(symbol, direction)

        # ── Check 3: currency exposure (count, direction-agnostic) ───────
        for cur in prop_legs:
            involved = sum(1 for p in positions if cur in self._signed_legs(p[0], p[1]))
            if involved + 1 > cfg.max_currency_exposure:
                return self._block(
                    symbol,
                    direction,
                    "currency_exposure",
                    f"{cur} exposure limit — {involved} open position(s) already involve "
                    f"{cur} (max {cfg.max_currency_exposure})",
                )

        # ── Check 4: sector / category exposure ──────────────────────────
        prop_cat = self._category(symbol)
        if prop_cat:
            same_cat = sum(1 for p in positions if self._category(p[0]) == prop_cat)
            if same_cat + 1 > cfg.max_sector_exposure:
                return self._block(
                    symbol,
                    direction,
                    "sector_exposure",
                    f"{prop_cat} sector limit — {same_cat} open position(s) already in "
                    f"{prop_cat} (max {cfg.max_sector_exposure})",
                )

        # ── Check 5: correlated positions (same signed currency exposure) ─
        correlated = 0
        for p in positions:
            p_legs = self._signed_legs(p[0], p[1])
            if any(prop_legs.get(c) == s for c, s in p_legs.items()):
                correlated += 1
        if correlated + 1 > cfg.max_correlated_positions:
            return self._block(
                symbol,
                direction,
                "correlated_positions",
                f"correlated exposure limit — {correlated} open position(s) share signed "
                f"currency exposure (max {cfg.max_correlated_positions})",
            )

        return GovernorVerdict(
            allowed=True,
            reason=(
                f"portfolio clear ({len(positions)}/{cfg.max_open_positions} open, daily {self._daily_pnl_pct():.2f}%)"
            ),
        )

    # ── Daily P&L tracking ───────────────────────────────────────────────

    def update_daily_pnl(self, pnl_change: float) -> None:
        """Fold a realised P&L change into the running daily tally."""
        try:
            self.daily_pnl += float(pnl_change)
        except (TypeError, ValueError):
            return
        self._evaluate_halt()

    def reset_daily(self) -> None:
        """Clear the daily tally and lift any loss-cap halt (called on daily reset)."""
        self.daily_pnl = 0.0
        if self.daily_trading_halted:
            logger.info("[Governor] daily reset — loss-cap halt lifted")
        self.daily_trading_halted = False

    def set_reference_balance(self, balance: float) -> None:
        if balance and balance > 0:
            self._reference_balance = balance

    def _daily_pnl_pct(self) -> float:
        if self._reference_balance <= 0:
            return 0.0
        return self.daily_pnl / self._reference_balance * 100.0

    def _evaluate_halt(self) -> None:
        """Apply hysteresis: halt at the cap, resume only after recovery."""
        if self._reference_balance <= 0:
            return
        pct = self._daily_pnl_pct()
        cfg = self.config
        if self.daily_trading_halted:
            if pct >= -cfg.daily_loss_recovery_pct:
                self.daily_trading_halted = False
                logger.info(
                    "[Governor] daily loss recovered to {:.2f}% — resuming entries",
                    pct,
                )
        elif pct <= -cfg.daily_loss_cap_pct:
            self.daily_trading_halted = True
            logger.info(
                "[Governor] HALTED — daily loss {:.2f}% breached −{:.1f}% cap",
                pct,
                cfg.daily_loss_cap_pct,
            )

    # ── Dashboard state ──────────────────────────────────────────────────

    def get_state(self, open_positions: list[Any] | None = None) -> dict:
        """Snapshot for the dashboard — exposure breakdowns + recent blocks."""
        positions = [self._normalize(p) for p in (open_positions or [])]
        positions = [p for p in positions if p is not None]

        currency_exposure: dict[str, int] = {}
        sector_exposure: dict[str, int] = {}
        for sym, direction in positions:
            for cur in self._signed_legs(sym, direction):
                currency_exposure[cur] = currency_exposure.get(cur, 0) + 1
            cat = self._category(sym)
            if cat:
                sector_exposure[cat] = sector_exposure.get(cat, 0) + 1

        cfg = self.config
        return {
            "enabled": cfg.enabled,
            "trading_halted": self.daily_trading_halted,
            "daily_pnl": round(self.daily_pnl, 2),
            "daily_pnl_pct": round(self._daily_pnl_pct(), 3),
            "daily_loss_cap_pct": cfg.daily_loss_cap_pct,
            "daily_loss_recovery_pct": cfg.daily_loss_recovery_pct,
            "open_positions": len(positions),
            "max_open_positions": cfg.max_open_positions,
            "max_currency_exposure": cfg.max_currency_exposure,
            "max_sector_exposure": cfg.max_sector_exposure,
            "max_correlated_positions": cfg.max_correlated_positions,
            "currency_exposure": dict(sorted(currency_exposure.items(), key=lambda kv: -kv[1])),
            "sector_exposure": dict(sorted(sector_exposure.items(), key=lambda kv: -kv[1])),
            "recent_blocks": list(self._recent_blocks)[::-1],  # newest first
        }

    # ── Internal helpers ─────────────────────────────────────────────────

    def _block(self, symbol: str, direction: str, blocked_by: str, reason: str) -> GovernorVerdict:
        logger.info("[Governor] BLOCK {} {} — {}", direction, symbol, reason)
        self._recent_blocks.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "direction": direction,
                "blocked_by": blocked_by,
                "reason": reason,
            }
        )
        return GovernorVerdict(allowed=False, reason=reason, blocked_by=blocked_by)

    @staticmethod
    def _normalize(pos: Any) -> tuple[str, str] | None:
        """Extract (symbol, direction) from a ManagedPosition, OpenTrade, dict
        or a plain (symbol, direction) tuple/list."""
        try:
            if isinstance(pos, (tuple, list)):
                if len(pos) < 2:
                    return None
                sym, direction = pos[0], pos[1]
            elif isinstance(pos, dict):
                sym = pos.get("pair") or pos.get("symbol") or ""
                direction = pos.get("direction") or ""
            else:
                sym = getattr(pos, "symbol", "") or getattr(pos, "pair", "")
                direction = getattr(pos, "direction", "")
            sym = str(sym).upper().strip()
            direction = str(direction).upper().strip()
            if not sym:
                return None
            return (sym, direction)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _is_long(direction: str) -> bool:
        return str(direction).upper() in ("BUY", "LONG")

    def _signed_legs(self, symbol: str, direction: str) -> dict[str, int]:
        """Signed currency exposure for a position.

        Long base/quote → base +1, quote −1.  Short → reversed.  Indices and
        unparsable symbols return an empty map (handled via sector exposure).
        """
        sym = str(symbol).upper().strip()
        legs: dict[str, int] = {}
        if len(sym) == 6:
            base, quote = sym[:3], sym[3:6]
            if base in _KNOWN_CURRENCIES and quote in _KNOWN_CURRENCIES:
                sign = 1 if self._is_long(direction) else -1
                legs[base] = sign
                legs[quote] = -sign
        return legs

    @staticmethod
    def _category(symbol: str) -> str:
        """Instrument category from the registry ('forex'/'index'/…) or ''."""
        try:
            from config import INSTRUMENT_REGISTRY  # lazy — avoids import cycle

            info = INSTRUMENT_REGISTRY.get(str(symbol).upper())
            if info is not None:
                return info.category.value
        except Exception:  # noqa: BLE001
            return ""
        return ""
