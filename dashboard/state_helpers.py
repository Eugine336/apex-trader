"""
APEX TRADER — Dashboard State Helpers
Shared utility functions and the HelpersMixin used by all state sub-modules.
"""

import asyncio
import time as _time
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from config import INSTRUMENT_REGISTRY, get_instrument, get_pip_size


# ── Pure helpers (no self) ──────────────────────────────────────────────


def value(obj: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if isinstance(obj, dict) and key in obj:
            return obj[key]
        if hasattr(obj, key):
            return getattr(obj, key)
    return default


def safe_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None:
            return default
        return float(val)
    except Exception:
        return default


def pct_to_fraction(val: Any) -> float:
    raw = safe_float(val, 0.0)
    return raw / 100.0 if abs(raw) > 1 else raw


def normalize_direction(direction: Any) -> str:
    raw = str(direction or "").upper()
    if raw in {"BUY", "LONG"}:
        return "LONG"
    if raw in {"SELL", "SHORT"}:
        return "SHORT"
    return "NEUTRAL"


def parse_dt(val: Any) -> Optional[datetime]:
    if isinstance(val, datetime):
        return val
    if not val:
        return None
    try:
        text = str(val).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except Exception:
        return None


def build_stage(pos: Any) -> str:
    if bool(getattr(pos, "trailing", False)):
        return "TRAILING"
    if bool(getattr(pos, "at_breakeven", False)):
        return "BREAKEVEN"
    if bool(getattr(pos, "tp1_hit", False)):
        return "TP1_HIT"
    return "OPEN"


def base_factor_set() -> dict[str, int]:
    return {
        "structure": 0,
        "fvg": 0,
        "ob": 0,
        "liquidity": 0,
        "sweep": 0,
        "session": 0,
        "strength": 0,
    }


# ── HelpersMixin (instance methods that need self._*) ──────────────────


class HelpersMixin:
    """Methods shared across all dashboard state sub-modules."""

    _trading_loop: Any
    _platform_manager: Any
    _journal_cache: list
    _journal_cache_ts: float
    _journal_cache_ttl: float

    @property
    def is_live(self) -> bool: ...  # provided by LiveState

    def _refresh_journal_cache(self) -> list[dict]:
        now = _time.monotonic()
        if now - self._journal_cache_ts < self._journal_cache_ttl:
            return self._journal_cache

        journal = getattr(self._trading_loop, "journal", None) if self.is_live else None
        if journal is None:
            return self._journal_cache

        try:
            import concurrent.futures

            def _run_in_thread():
                thread_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(thread_loop)
                try:
                    return thread_loop.run_until_complete(journal.get_all_trades_as_dicts())
                finally:
                    thread_loop.close()

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                rows: list[dict] = ex.submit(_run_in_thread).result(timeout=5.0)

            self._journal_cache = rows
            self._journal_cache_ts = now
        except Exception as exc:
            logger.warning("Journal cache refresh failed: {}", exc)

        return self._journal_cache

    def _get_balance(self) -> float:
        if not self.is_live:
            return 10000.0
        try:
            bal = self._platform_manager.get_total_balance()
            return round(float(bal), 2) if bal else 10000.0
        except Exception:
            return 10000.0

    def _get_platform_balances(self) -> tuple[float, float]:
        if not self.is_live:
            return 0.0, 0.0
        try:
            summary = self._platform_manager.get_account_summary()
            mt5_info = summary.get("mt5")
            deriv_info = summary.get("deriv")
            mt5_balance = safe_float(getattr(mt5_info, "balance", 0.0), 0.0)
            deriv_balance = safe_float(getattr(deriv_info, "balance", 0.0), 0.0)
            return round(mt5_balance, 2), round(deriv_balance, 2)
        except Exception:
            return 0.0, 0.0

    def _estimate_pip_value(self, symbol: str) -> float:
        try:
            return float(get_instrument(symbol).pip_value_per_lot)
        except Exception:
            return 10.0

    def _build_history_rows(self, balance: float) -> list[dict[str, Any]]:
        raw_rows = self._refresh_journal_cache()
        rows: list[dict[str, Any]] = []

        for i, record in enumerate(raw_rows):
            symbol = str(record.get("pair", "")).upper()
            direction = normalize_direction(record.get("direction", ""))
            entry_price = safe_float(record.get("entry", record.get("entry_price", 0.0)))
            exit_price = safe_float(record.get("exit", record.get("exit_price", entry_price)))

            pnl_dollars_raw = record.get("pnl_dollars")
            if pnl_dollars_raw is not None and pnl_dollars_raw != 0.0:
                pnl_dollars = safe_float(pnl_dollars_raw, 0.0)
            else:
                pnl_dollars = safe_float(record.get("pnl", 0.0))

            pnl_pips_raw = safe_float(record.get("pnl", 0.0))

            pip_size = safe_float(get_pip_size(symbol), 0.0001) or 0.0001
            sl_distance = abs(exit_price - entry_price)
            pnl_pips = pnl_pips_raw if pnl_pips_raw != 0.0 else (sl_distance / pip_size if sl_distance > 0 else 0.0)
            if pnl_dollars < 0 and pnl_pips > 0:
                pnl_pips = -pnl_pips

            time_to_exit = safe_float(record.get("time_to_exit"), 0.0)
            duration_minutes = time_to_exit / 60.0 if time_to_exit > 0 else 0.0

            outcome = str(record.get("outcome", "")).upper()
            if outcome not in {"WIN", "LOSS"}:
                outcome = "WIN" if pnl_dollars >= 0 else "LOSS"

            ts = record.get("timestamp", datetime.now(timezone.utc).isoformat())
            opened_at = str(ts) if ts else datetime.now(timezone.utc).isoformat()

            rows.append(
                {
                    "id": str(record.get("id", f"hist_{i}")),
                    "instrument": symbol,
                    "direction": direction,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "pnl_pips": round(pnl_pips, 1),
                    "pnl_dollars": round(pnl_dollars, 2),
                    "duration_minutes": round(duration_minutes, 1),
                    "score": int(round(safe_float(record.get("score", 0)))),
                    "outcome": outcome,
                    "opened_at": opened_at,
                }
            )

        return rows

    def _scanner_factors(self, result: Any) -> dict[str, int]:
        bias_strength = str(value(result, "bias_strength", default="")).upper()
        structure_pts = 15 if bias_strength == "STRONG" else 10 if bias_strength == "MODERATE" else 5

        return {
            "structure": structure_pts,
            "fvg": 10 if bool(value(result, "has_fvg", default=False)) else 0,
            "ob": 10 if bool(value(result, "has_order_block", default=False)) else 0,
            "liquidity": 10 if bool(value(result, "has_liquidity_target", default=False)) else 0,
            "sweep": 10 if bool(value(result, "sweep_detected", default=False)) else 0,
            "session": 10 if bool(value(result, "session_active", default=False)) else 0,
            "strength": 10 if bool(value(result, "currency_strength_aligned", default=False)) else 0,
        }
