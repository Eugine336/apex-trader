"""
APEX TRADER — Execution Monitor
Signal quality means nothing without fill quality.
This module tracks latency, slippage, spread, and requotes in real time,
per-symbol and windowed to stay sensitive to recent execution changes.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime
import math
import time

import numpy as np


@dataclass
class ExecutionStats:
    avg_slippage_pips: float
    max_slippage_pips: float
    avg_latency_ms: float
    spread_current: float
    spread_average: float
    spread_is_wide: bool
    requote_count: int
    execution_quality: str


_QUALITY_MULTIPLIERS: dict[str, float] = {
    "EXCELLENT": 1.0,
    "GOOD": 1.0,
    "POOR": 0.5,
    "UNACCEPTABLE": 0.25,
}


class ExecutionMonitor:
    """
    Tracks execution friction to ensure the strategy edge survives in real fills.
    Per-symbol windowed buffers keep the monitor sensitive to recent changes.
    """

    def __init__(
        self,
        pip_size: float = 0.0001,
        slippage_alert_pips: float = 1.2,
        wide_spread_multiplier: float = 1.8,
        window: int = 200,
    ):
        self.pip_size = pip_size
        self.slippage_alert_pips = slippage_alert_pips
        self.wide_spread_multiplier = wide_spread_multiplier
        self._window = max(window, 1)
        self._slippages: dict[str, deque] = {}
        self._latencies: dict[str, deque] = {}
        self._spreads: dict[str, deque] = {}
        self._requotes: dict[str, int] = {}
        self._order_attempts: dict[str, deque] = {}
        # Per-ticket entry/exit pairing is observational and bounded by currently
        # open broker tickets; completed samples use the same rolling window.
        self._open_trade_telemetry: dict[str, dict] = {}
        self._round_trip_slippages: dict[str, deque] = {}

    def _ensure_symbol(self, symbol: str) -> None:
        if symbol not in self._slippages:
            self._slippages[symbol] = deque(maxlen=self._window)
            self._latencies[symbol] = deque(maxlen=self._window)
            self._spreads[symbol] = deque(maxlen=self._window)
            self._requotes[symbol] = 0
            self._order_attempts[symbol] = deque(maxlen=self._window)
            self._round_trip_slippages[symbol] = deque(maxlen=self._window)

    def record_order_attempt(
        self,
        *,
        symbol: str,
        success: bool,
        latency_ms: float | None,
        requested_price: float | None = None,
        fill_price: float | None = None,
        pip_size: float | None = None,
        spread: float | None = None,
        observed_epoch: float | None = None,
        order_type: str = "market",
        order_id: str | None = None,
        lots: float | None = None,
        direction: str | None = None,
        platform: str = "unknown",
    ) -> None:
        """Record one broker market-entry attempt; this does not affect execution.

        Latency is submit-to-response time, not signal-to-fill time. Slippage is
        recorded only with both prices and a valid instrument pip size.
        """
        clean_symbol = str(symbol or "").strip().upper()
        if not clean_symbol:
            return

        def finite_number(value: object, *, positive: bool = False) -> float | None:
            if isinstance(value, bool):
                return None
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if not math.isfinite(number) or number < 0.0 or (positive and number <= 0.0):
                return None
            return number

        observed = finite_number(observed_epoch, positive=True) or time.time()
        latency = finite_number(latency_ms)
        requested = finite_number(requested_price, positive=True)
        filled = finite_number(fill_price, positive=True)
        pip = finite_number(pip_size, positive=True)
        spread_value = finite_number(spread)
        slippage = None
        confirmed = success is True
        if confirmed and requested is not None and filled is not None and pip is not None:
            slippage = abs(filled - requested) / pip
            if not math.isfinite(slippage):
                slippage = None

        self._ensure_symbol(clean_symbol)
        record = {
            "observed_epoch": observed,
            "success": confirmed,
            "latency_ms": latency,
            "slippage_pips": slippage,
            "spread": spread_value,
            "order_type": str(order_type or "market").strip().lower(),
        }
        self._order_attempts[clean_symbol].append(record)
        clean_order_id = str(order_id or "").strip()
        clean_platform = str(platform or "unknown").strip().lower().split("_", 1)[0]
        volume = finite_number(lots, positive=True)
        if (
            confirmed and clean_order_id and volume is not None
            and pip is not None
        ):
            ticket_key = f"{clean_platform}:{clean_order_id}"
            self._open_trade_telemetry[ticket_key] = {
                "symbol": clean_symbol,
                "open_lots": volume,
                "closed_lots": 0.0,
                "entry_slippage_pips": slippage,
                "pip_size": pip,
                "exit_slippage_weighted_pips": 0.0,
                "exit_slippage_lots": 0.0,
                "round_trip_valid": slippage is not None,
                "direction": str(direction or "").strip().upper(),
                "opened_epoch": observed,
            }
        if latency is not None:
            self._latencies[clean_symbol].append(latency)
        if slippage is not None:
            self._slippages[clean_symbol].append(slippage)
        if spread_value is not None:
            self._spreads[clean_symbol].append(spread_value)

    def record_close_result(
        self,
        *,
        order_id: str,
        platform: str,
        success: bool,
        requested_price: float | None,
        fill_price: float | None,
        lots_closed: float | None,
        observed_epoch: float | None = None,
    ) -> None:
        """Pair an attributable broker close fill with its recorded entry fill.

        Close slippage is measured against the exact price submitted by the
        connector. A complete sample is emitted only after the entire recorded
        position volume has closed and every partial close had valid prices.
        Payout values without a comparable requested market price remain unknown.
        """
        if success is not True:
            return
        clean_order_id = str(order_id or "").strip()
        clean_platform = str(platform or "unknown").strip().lower().split("_", 1)[0]
        if not clean_order_id:
            return
        ticket_key = f"{clean_platform}:{clean_order_id}"
        trade = self._open_trade_telemetry.get(ticket_key)
        if not isinstance(trade, dict):
            return

        def finite_number(value: object, *, positive: bool = False) -> float | None:
            if isinstance(value, bool):
                return None
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if not math.isfinite(number) or number < 0.0 or (positive and number <= 0.0):
                return None
            return number

        volume = finite_number(lots_closed, positive=True)
        if volume is None:
            trade["round_trip_valid"] = False
            return
        remaining = max(float(trade["open_lots"]) - float(trade["closed_lots"]), 0.0)
        tolerance = max(1e-8, float(trade["open_lots"]) * 1e-6)
        if volume > remaining + tolerance:
            trade["round_trip_valid"] = False
            return

        requested = finite_number(requested_price, positive=True)
        filled = finite_number(fill_price, positive=True)
        pip = finite_number(trade.get("pip_size"), positive=True)
        close_slippage = None
        if requested is not None and filled is not None and pip is not None:
            close_slippage = abs(filled - requested) / pip
            if not math.isfinite(close_slippage):
                close_slippage = None

        if close_slippage is None:
            trade["round_trip_valid"] = False
        else:
            trade["exit_slippage_weighted_pips"] += close_slippage * volume
            trade["exit_slippage_lots"] += volume

        trade["closed_lots"] += volume
        if float(trade["closed_lots"]) + tolerance < float(trade["open_lots"]):
            return

        completed_epoch = finite_number(observed_epoch, positive=True) or time.time()
        all_exit_volume_measured = (
            float(trade["exit_slippage_lots"]) + tolerance >= float(trade["open_lots"])
        )
        if trade.get("round_trip_valid") is True and all_exit_volume_measured:
            avg_exit_slippage = (
                float(trade["exit_slippage_weighted_pips"])
                / float(trade["exit_slippage_lots"])
            )
            total_slippage = float(trade["entry_slippage_pips"]) + avg_exit_slippage
            if math.isfinite(total_slippage):
                symbol = str(trade["symbol"]).strip().upper()
                self._ensure_symbol(symbol)
                self._round_trip_slippages[symbol].append({
                    "observed_epoch": completed_epoch,
                    "slippage_pips": total_slippage,
                    "pip_size": float(trade["pip_size"]),
                    "order_id": clean_order_id,
                    "platform": clean_platform,
                    "lots": float(trade["open_lots"]),
                })

        # Once the broker reports the full volume closed, retain only the
        # completed aggregate. An unmeasurable exit is discarded, not guessed.
        self._open_trade_telemetry.pop(ticket_key, None)

    def get_cost_observations(
        self,
        symbol: str,
        *,
        now_epoch: float | None = None,
        max_age_seconds: float = 604800.0,
        min_samples: int = 20,
    ) -> dict:
        """Expose recent attempts with explicit sample and unknown states.

        A low-sample success ratio is descriptive only. The fill-rate measure is
        marked empirically supported only after min_samples broker attempts.
        The canonical route currently records market entries.
        """
        clean_symbol = str(symbol or "").strip().upper()
        try:
            now = float(now_epoch) if now_epoch is not None else time.time()
            if not math.isfinite(now) or now <= 0.0:
                now = time.time()
        except (TypeError, ValueError, OverflowError):
            now = time.time()
        try:
            max_age = max(0.0, float(max_age_seconds))
        except (TypeError, ValueError, OverflowError):
            max_age = 604800.0
        try:
            sample_floor = max(1, int(min_samples))
        except (TypeError, ValueError, OverflowError):
            sample_floor = 20

        attempts = list(self._order_attempts.get(clean_symbol, ()))
        outcomes = [
            item for item in attempts
            if item.get("order_type") == "market"
            and 0.0 <= now - float(item.get("observed_epoch", 0.0)) <= max_age
        ]
        success_count = sum(1 for item in outcomes if item.get("success") is True)
        attempt_count = len(outcomes)
        slippages = [
            float(item["slippage_pips"]) for item in outcomes
            if item.get("success") is True and item.get("slippage_pips") is not None
        ]
        latencies = [float(item["latency_ms"]) for item in outcomes if item.get("latency_ms") is not None]
        spreads = [float(item["spread"]) for item in outcomes if item.get("spread") is not None]
        round_trip_samples = [
            item for item in self._round_trip_slippages.get(clean_symbol, ())
            if 0.0 <= now - float(item.get("observed_epoch", 0.0)) <= max_age
        ]
        pip_sizes = [
            float(item["pip_size"]) for item in round_trip_samples
            if item.get("pip_size") is not None
        ]
        common_pip_size = (
            pip_sizes[0]
            if pip_sizes and all(abs(value - pip_sizes[0]) <= 1e-12 for value in pip_sizes)
            else None
        )
        round_trip_floor_met = (
            len(round_trip_samples) >= sample_floor and common_pip_size is not None
        )
        return {
            "market_entry_attempt_count": attempt_count,
            "market_entry_success_count": success_count,
            "market_entry_fill_success_ratio": success_count / attempt_count if attempt_count else None,
            "market_entry_fill_rate_sample_floor_met": attempt_count >= sample_floor,
            "market_entry_fill_probability_min_samples": sample_floor,
            "market_entry_fill_probability_sample_count": attempt_count,
            "observed_entry_abs_slippage_avg_pips": float(np.mean(slippages)) if slippages else None,
            "observed_entry_abs_slippage_max_pips": float(np.max(slippages)) if slippages else None,
            "observed_entry_slippage_sample_count": len(slippages),
            "observed_submit_response_latency_avg_ms": float(np.mean(latencies)) if latencies else None,
            "observed_submit_response_latency_sample_count": len(latencies),
            "observed_entry_spread_avg_price": float(np.mean(spreads)) if spreads else None,
            "observed_entry_spread_sample_count": len(spreads),
            "entry_observation_latest_epoch": max(
                (float(item["observed_epoch"]) for item in outcomes), default=None,
            ),
            "entry_observation_max_age_seconds": max_age,
            "entry_observation_scope": "market_entry_attempts_only",
            "observed_round_trip_slippage_sample_count": len(round_trip_samples),
            "observed_round_trip_abs_slippage_avg_pips": (
                float(np.mean([item["slippage_pips"] for item in round_trip_samples]))
                if round_trip_samples else None
            ),
            "round_trip_slippage_verified": round_trip_floor_met,
            "pip_size": common_pip_size,
            "round_trip_slippage_scope": "paired_completed_broker_entry_and_exit_fills",
        }

    def record_execution(
        self,
        requested_price: float,
        filled_price: float,
        signal_timestamp: datetime,
        fill_timestamp: datetime,
        spread: float,
        requote: bool = False,
        pip_size: float | None = None,
        symbol: str = "_global",
    ) -> None:
        actual_pip = pip_size if pip_size and pip_size > 0 else self.pip_size
        slippage_pips = abs(filled_price - requested_price) / actual_pip
        latency_ms = max(
            (fill_timestamp - signal_timestamp).total_seconds() * 1000.0, 0.0
        )

        self._ensure_symbol(symbol)
        self._slippages[symbol].append(float(slippage_pips))
        self._latencies[symbol].append(float(latency_ms))
        self._spreads[symbol].append(float(spread))
        if requote:
            self._requotes[symbol] += 1

    def _aggregate_deques(self, store: dict[str, deque]) -> list[float]:
        result: list[float] = []
        for dq in store.values():
            result.extend(dq)
        return result

    def get_stats(self, symbol: str | None = None) -> ExecutionStats:
        if symbol and symbol in self._slippages:
            slips = list(self._slippages[symbol])
            lats = list(self._latencies[symbol])
            sprs = list(self._spreads[symbol])
            reqs = self._requotes.get(symbol, 0)
        else:
            slips = self._aggregate_deques(self._slippages)
            lats = self._aggregate_deques(self._latencies)
            sprs = self._aggregate_deques(self._spreads)
            reqs = sum(self._requotes.values())

        avg_slippage = float(np.mean(slips)) if slips else 0.0
        max_slippage = float(np.max(slips)) if slips else 0.0
        avg_latency = float(np.mean(lats)) if lats else 0.0
        spread_current = float(sprs[-1]) if sprs else 0.0
        spread_average = float(np.mean(sprs)) if sprs else 0.0

        spread_is_wide = (
            spread_average > 0
            and spread_current > spread_average * self.wide_spread_multiplier
        )
        quality = self._grade_execution(avg_slippage, avg_latency, spread_is_wide)

        return ExecutionStats(
            avg_slippage_pips=round(avg_slippage, 4),
            max_slippage_pips=round(max_slippage, 4),
            avg_latency_ms=round(avg_latency, 2),
            spread_current=round(spread_current, 6),
            spread_average=round(spread_average, 6),
            spread_is_wide=spread_is_wide,
            requote_count=reqs,
            execution_quality=quality,
        )

    def should_alert(self) -> bool:
        for sym in self._slippages:
            stats = self.get_stats(symbol=sym)
            if (
                stats.avg_slippage_pips >= self.slippage_alert_pips
                or stats.execution_quality in {"POOR", "UNACCEPTABLE"}
                or stats.spread_is_wide
            ):
                return True
        return False

    def get_typical_spread(self, symbol: str) -> float | None:
        if symbol not in self._spreads or not self._spreads[symbol]:
            return None
        return float(np.mean(list(self._spreads[symbol])))

    def get_size_multiplier(self, symbol: str) -> float:
        if symbol not in self._slippages or not self._slippages[symbol]:
            return 1.0
        stats = self.get_stats(symbol=symbol)
        return _QUALITY_MULTIPLIERS.get(stats.execution_quality, 1.0)

    def _grade_execution(
        self, avg_slippage: float, avg_latency: float, spread_is_wide: bool
    ) -> str:
        if spread_is_wide or avg_slippage > 1.5 or avg_latency > 750:
            return "UNACCEPTABLE"
        if avg_slippage > 1.0 or avg_latency > 450:
            return "POOR"
        if avg_slippage > 0.5 or avg_latency > 220:
            return "GOOD"
        return "EXCELLENT"
