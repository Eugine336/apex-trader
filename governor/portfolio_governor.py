"""
APEX TRADER — Portfolio Governor.

Per-trade risk lives in `risk.risk_engine`.  The Governor adds the missing
*portfolio-level* layer: it stops the system opening too many correlated
positions, over-concentrating in one currency or sector, exceeding a hard
position cap, or trading on after a bad day.

Design rules:
  * Advisory to the planner — it returns a verdict, it is NOT a hard gate in
    the execution layer.  Defence in depth, not a single point of failure.
  * Fail-closed by default — if the governor code raises, the trade is BLOCKED
    (a portfolio-risk veto that silently no-ops on a bug can't be trusted). Set
    ``GovernorConfig.fail_closed=False`` to restore legacy fail-open behaviour.
  * Every block is logged at INFO (these are important safety events) and
    retained in a short ring buffer for the dashboard.
  * All thresholds come from `GovernorConfig` — no magic numbers.

Leaf-ish module — imports `config` lazily inside methods to avoid an
import cycle (config imports GovernorConfig from governor.models).
"""

from __future__ import annotations

import re
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

        Fail-closed by default: any internal error returns a *blocked* verdict so
        a crashing safety advisor cannot silently let risk through. Set
        ``GovernorConfig.fail_closed=False`` to restore legacy fail-open.
        """
        try:
            return self._check_inner(symbol, direction, open_positions or [], account_balance)
        except Exception as exc:  # noqa: BLE001
            if not getattr(self.config, "fail_closed", True):
                logger.warning(
                    "[Governor] check error for {} {} — failing OPEN (allowing): {}",
                    direction,
                    symbol,
                    exc,
                )
                return GovernorVerdict(allowed=True, reason=f"governor error (fail-open): {exc}")
            logger.error(
                "[Governor] check error for {} {} — failing CLOSED (blocking): {}",
                direction,
                symbol,
                exc,
            )
            return self._block(
                symbol,
                direction,
                "governor_error",
                f"governor error (fail-closed): {exc}",
            )

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

        # ── Check 1: daily loss cap (HARD safety halt — never graded) ────
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

        # ── Check 2: max open positions (HARD physics cap — never graded) ─
        if len(positions) >= cfg.max_open_positions:
            return self._block(
                symbol,
                direction,
                "max_positions",
                f"max open positions reached ({len(positions)}/{cfg.max_open_positions})",
            )

        prop_legs = self._signed_legs(symbol, direction)

        # ── Analytical concentration limits (currency / sector / correlated) ─
        # Legacy: first-breach hard block.  Graded (#24): measure ALL three and
        # fold into a bounded risk multiplier instead of killing on the first.
        graded = bool(getattr(cfg, "graded_exposure", False))

        # Currency / group exposure (count, direction-agnostic).
        currency_worst: tuple[int, str] | None = None  # (count, currency)
        for cur in prop_legs:
            involved = sum(1 for p in positions if cur in self._signed_legs(p[0], p[1]))
            projected = involved + 1
            if currency_worst is None or projected > currency_worst[0]:
                currency_worst = (projected, cur)
            if not graded and projected > cfg.max_currency_exposure:
                return self._block(
                    symbol,
                    direction,
                    "currency_exposure",
                    f"{cur} exposure limit — {involved} open position(s) already involve "
                    f"{cur} (max {cfg.max_currency_exposure})",
                )

        # Sector / category exposure.
        prop_cat = self._category(symbol)
        sector_projected = 0
        if prop_cat:
            same_cat = sum(1 for p in positions if self._category(p[0]) == prop_cat)
            sector_projected = same_cat + 1
            if not graded and sector_projected > cfg.max_sector_exposure:
                return self._block(
                    symbol,
                    direction,
                    "sector_exposure",
                    f"{prop_cat} sector limit — {same_cat} open position(s) already in "
                    f"{prop_cat} (max {cfg.max_sector_exposure})",
                )

        # Correlated positions (same signed currency / group exposure).
        correlated = 0
        for p in positions:
            p_legs = self._signed_legs(p[0], p[1])
            if any(prop_legs.get(c) == s for c, s in p_legs.items()):
                correlated += 1
        correlated_projected = correlated + 1
        if not graded and correlated_projected > cfg.max_correlated_positions:
            return self._block(
                symbol,
                direction,
                "correlated_positions",
                f"correlated exposure limit — {correlated} open position(s) share signed "
                f"currency exposure (max {cfg.max_correlated_positions})",
            )

        if graded:
            return self._graded_verdict(
                symbol,
                direction,
                len(positions),
                currency_worst,
                sector_projected,
                prop_cat,
                correlated_projected,
            )

        return GovernorVerdict(
            allowed=True,
            reason=(
                f"portfolio clear ({len(positions)}/{cfg.max_open_positions} open, daily {self._daily_pnl_pct():.2f}%)"
            ),
        )

    def _graded_verdict(
        self,
        symbol: str,
        direction: str,
        open_count: int,
        currency_worst: "tuple[int, str] | None",
        sector_projected: int,
        sector_cat: str,
        correlated_projected: int,
    ) -> GovernorVerdict:
        """Accumulate the analytical concentration limits into one graded verdict.

        Physics (max positions, daily cap) were already enforced as hard blocks
        above; here the currency / sector / correlated exposures fold into a
        bounded ``risk_multiplier`` so a near-/over-limit concentration sizes the
        trade DOWN instead of killing it.
        """
        cfg = self.config
        # Lazy import — keeps the ``risk`` package (and its heavy ``risk_engine``
        # → ``config`` import) off the module-load path, since ``config`` itself
        # imports ``governor`` and would otherwise cycle.
        from risk import risk_accumulation as ra

        dims: list[ra.RiskDimension] = []
        if currency_worst is not None:
            cnt, cur = currency_worst
            dims.append(ra.dimension(
                "currency_exposure", cnt, cfg.max_currency_exposure,
                f"{cur} touched by {cnt} position(s) vs max {cfg.max_currency_exposure}",
            ))
        if sector_cat:
            dims.append(ra.dimension(
                "sector_exposure", sector_projected, cfg.max_sector_exposure,
                f"{sector_cat} ×{sector_projected} vs max {cfg.max_sector_exposure}",
            ))
        dims.append(ra.dimension(
            "correlated_positions", correlated_projected, cfg.max_correlated_positions,
            f"correlated ×{correlated_projected} vs max {cfg.max_correlated_positions}",
        ))

        score = ra.accumulate(
            dims, floor=float(getattr(cfg, "risk_multiplier_floor", 0.15)),
        )
        near = score.near_breaches + score.breaches
        reason = (
            f"portfolio graded ({open_count}/{cfg.max_open_positions} open, daily "
            f"{self._daily_pnl_pct():.2f}%) — risk ×{score.multiplier:.2f}"
            + (f" near/over: {', '.join(near)}" if near else "")
        )
        if score.multiplier < 1.0 - 1e-9:
            logger.info(
                "[Governor] GRADED {} {} — risk ×{:.2f} ({})",
                direction, symbol, score.multiplier,
                ", ".join(near) if near else "all clear",
            )
        return GovernorVerdict(
            allowed=True,
            reason=reason,
            risk_multiplier=score.multiplier,
            risk_score=score.score,
            near_breaches=near,
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

    def to_state(self) -> dict:
        """Serialise the daily tally + halt so a restart doesn't lose the day's
        loss budget."""
        return {
            "daily_pnl": self.daily_pnl,
            "daily_trading_halted": self.daily_trading_halted,
            "reference_balance": self._reference_balance,
        }

    def restore_state(self, state: dict) -> None:
        """Restore the daily tally + halt from a persisted payload."""
        try:
            self.daily_pnl = float(state.get("daily_pnl", 0.0) or 0.0)
            self.daily_trading_halted = bool(state.get("daily_trading_halted", False))
            ref = float(state.get("reference_balance", 0.0) or 0.0)
            if ref > 0:
                self._reference_balance = ref
        except (TypeError, ValueError) as exc:
            logger.warning(
                "[PortfolioGovernor] corrupt persisted state ignored — daily tally/"
                "halt start fresh this session: {}", exc,
            )

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
        """Signed exposure legs for a position.

        Forex (two known-currency legs): long base/quote → base +1, quote −1;
        short → reversed.  Non-forex (indices / crypto / metals / synthetics)
        used to return an empty map — making them INVISIBLE to the currency and
        correlated-exposure checks (#31).  They now emit a single signed *group*
        leg (e.g. ``IDX:US``, ``CRY:BTC``, ``VOL:V50``) so stacking several
        correlated indices / coins / volatility indices is seen and bounded by
        the same exposure limits as forex.
        """
        sym = str(symbol).upper().strip()
        legs: dict[str, int] = {}
        sign = 1 if self._is_long(direction) else -1
        if len(sym) == 6:
            base, quote = sym[:3], sym[3:6]
            if base in _KNOWN_CURRENCIES and quote in _KNOWN_CURRENCIES:
                legs[base] = sign
                legs[quote] = -sign
                return legs
        group = self._group_key(sym)
        if group:
            legs[group] = sign
        return legs

    def _group_key(self, symbol: str) -> str:
        """Correlation/exposure group for a NON-forex instrument (#31).

        Indices group by region/family, crypto by base coin, synthetics by
        volatility-index family.  Falls back to the registry category (so at
        least same-category stacking stays bounded) and finally the symbol
        itself, so an unmapped instrument can never stack invisibly.
        """
        sym = str(symbol).upper().strip()
        if not sym:
            return ""

        # ── Crypto: group by base coin (BTCUSD, ETH-USD, SOLUSDT → CRY:<coin>) ─
        for coin in ("BTC", "XBT", "ETH", "SOL", "XRP", "ADA", "DOT",
                     "BNB", "LTC", "DOGE", "AVAX", "MATIC", "LINK"):
            if sym.startswith(coin):
                canonical = "BTC" if coin == "XBT" else coin
                return f"CRY:{canonical}"

        # ── Indices: group by region / family ────────────────────────────
        index_families = {
            "US": ("US30", "US500", "US100", "SPX", "NAS", "NDX", "DJI",
                   "DOW", "WALL", "USTEC", "US2000", "RUSSELL"),
            "EU": ("GER", "DE30", "DE40", "DAX", "EU50", "STOXX", "ESP",
                   "FRA", "CAC", "ITA", "NED", "EUSTX"),
            "UK": ("UK100", "FTSE"),
            "JP": ("JP225", "JPN", "NIKKEI", "NIK"),
            "AU": ("AUS200", "ASX", "AU200"),
            "HK": ("HK50", "HKG", "HSI"),
            "CN": ("CN50", "CHINA", "CHN"),
        }
        for region, tokens in index_families.items():
            if any(tok in sym for tok in tokens):
                return f"IDX:{region}"

        # ── Synthetics (Deriv volatility indices) ─────────────────────────
        if "BOOM" in sym:
            return "VOL:BOOM"
        if "CRASH" in sym:
            return "VOL:CRASH"
        if sym.startswith("STEP") or "STEP" in sym:
            return "VOL:STEP"
        if "JUMP" in sym:
            return "VOL:JUMP"
        if sym.startswith("R_"):
            return f"VOL:{sym}"
        # V75, V50_1S, 1HZ50V, VOLATILITY100 … → VOL:<digits>
        m = re.search(r"(\d{2,4})", sym)
        if ("V" in sym or "VOL" in sym or "HZ" in sym) and m:
            return f"VOL:{m.group(1)}"

        # ── Metals / energy fallbacks not already parsed as currency legs ──
        if "GOLD" in sym or sym.startswith("XAU"):
            return "MET:XAU"
        if "SILVER" in sym or sym.startswith("XAG"):
            return "MET:XAG"
        if "OIL" in sym or sym.startswith("XTI") or sym.startswith("XBR") or "WTI" in sym or "BRENT" in sym:
            return "ENE:OIL"

        # ── Fallback: registry category, else the symbol itself ───────────
        cat = self._category(sym)
        if cat:
            return f"CAT:{cat}"
        return f"SYM:{sym}"

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
