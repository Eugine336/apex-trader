"""
APEX TRADER — MetaTrader 5 Connector
Connects to MetaTrader 5 via the official Python package.
Handles Forex, commodities, and indices on MT5.
"""

import platform as sys_platform
import time as _time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pandas as pd
from loguru import logger

from config import get_pip_size
from brain.symbol_mapper import SymbolMapper, resolve_to_internal
from ops.redaction import mask_account_id
from platforms.order_idempotency import build_order_comment, extract_idempotency_key
from platforms.base_connector import (
    AccountInfo,
    BaseConnector,
    CloseResult,
    DealCloseInfo,
    OrderResult,
    PositionInfo,
    TickData,
)

_MT5_AVAILABLE = False
try:
    import MetaTrader5 as mt5  # type: ignore[import-untyped]

    _MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore[assignment]


# Some symbols (commonly crypto / indices, e.g. BTCUSD) report
# trade_stops_level == 0 yet the broker still rejects stops placed inside the
# live spread/freeze zone with INVALID_STOPS. Floor the minimum stop distance
# at this multiple of the current spread so breakeven/profit-lock/trailing
# modifies are accepted instead of leaving the position unprotected.
_STOP_SPREAD_FLOOR_MULT = 1.5


_MT5_DEAL_REASON_MAP: dict[int, str] = {
    0: "MANUAL",           # DEAL_REASON_CLIENT
    1: "MANUAL",           # DEAL_REASON_MOBILE
    2: "MANUAL",           # DEAL_REASON_WEB
    3: "ALGO",             # DEAL_REASON_EXPERT
    4: "SL",               # DEAL_REASON_SL
    5: "TP",               # DEAL_REASON_TP
    6: "STOP_OUT",         # DEAL_REASON_SO
    7: "ROLLOVER",         # DEAL_REASON_ROLLOVER
    8: "VARIATION_MARGIN",  # DEAL_REASON_VMARGIN
    9: "SPLIT",            # DEAL_REASON_SPLIT
}


_TF_MAP: dict[str, Any] = {}
if _MT5_AVAILABLE:
    _TF_MAP = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }

_COMMON_SUFFIXES = ("", "m", ".raw", "#", ".ecn", ".stp", "_SB", ".pro", ".cash", "cash", "_i", "!")

# Fallback alias map — tried ONLY if broker JSON has no override and suffix scan fails.
# Covers the most common broker renaming patterns for indices and commodities.
_FALLBACK_ALIASES: dict[str, list[str]] = {
    "US100":  ["NAS100", "NASDAQ", "USTEC", "NDX100", "USTECH", "US100.cash"],
    "US30":   ["DJ30", "DOW30", "DOWJONES", "WALL30", "US30.cash"],
    "US500":  ["SP500", "SPX500", "S&P500", "US500.cash"],
    "GER40":  ["DAX40", "DAX", "DE40", "GER30", "GER40.cash", "DAX40.cash"],
    "UK100":  ["FTSE100", "FTSE", "UK100.cash"],
    "FRA40":  ["CAC40", "CAC", "FRA40.cash"],
    "ESP35":  ["IBEX35", "IBEX", "ESP35.cash"],
    "JP225":  ["JPN225", "NIKKEI", "N225", "JP225.cash"],
    "AUS200": ["ASX200", "AUS200.cash"],
    "HK50":   ["HSI50", "HANGSENG", "HK50.cash"],
    "XTIUSD": ["USOUSD", "USOIL", "WTI", "OIL"],
    "XBRUSD": ["UKOUSD", "UKOIL", "BRENT", "OIL.UK"],
}


class MT5Connector(BaseConnector):
    """MetaTrader 5 platform connector."""

    def __init__(
        self,
        login: int = 0,
        password: str = "",
        server: str = "",
        deviation: int = 20,
        magic: int = 202500,
        broker_name: str = "auto",  # "auto" = detect from terminal info on connect
        reject_on_minlot_inflation: bool = False,
        max_tick_age_seconds: float = 120.0,
        max_slippage_pips: float = 0.0,
        min_rr_after_adjust: float = 1.5,
        preserve_rr_after_adjust: bool = True,
    ):
        self._login = login
        self._password = password
        self._server = server
        self._deviation = deviation
        self._max_slippage_pips = float(max_slippage_pips)
        self._magic = magic
        self._connected = False
        self._symbol_cache: dict[str, Optional[str]] = {}  # None = confirmed not on broker
        self._not_found_warned: set[str] = set()  # warn once then silent
        self._broker_name = broker_name
        self._reject_on_minlot_inflation = reject_on_minlot_inflation
        # When the broker's minimum stop distance forces the SL/TP wider than
        # requested, the position sizer already sized for the tighter SL and the
        # approved R:R no longer exists. Reject the order if the post-adjustment
        # R:R falls below this floor rather than silently taking a worse trade.
        self._min_rr_after_adjust = float(min_rr_after_adjust)
        # When the broker's minimum stop distance widens the SL, the reward leg
        # (TP) is left where the sizer put it, collapsing the approved R:R. With
        # this enabled (default), the TP is extended outward to restore the
        # ORIGINAL R:R before the floor check rejects the order — extra reward
        # only, never added risk — so a tick-size rounding can't kill a setup
        # the entry pipeline already approved.
        self._preserve_rr_after_adjust = bool(preserve_rr_after_adjust)
        # Tick-freshness limit comes straight from config (RiskConfig resolves
        # the MAX_TICK_AGE_SECONDS env override explicitly at the config layer).
        self._max_tick_age_seconds = float(max_tick_age_seconds)
        # Defer SymbolMapper creation when broker_name is "auto".
        # connect() will detect the real broker name and create the mapper then.
        # Creating it now with "auto" would trigger a "no config" warning.
        self._mapper: Optional[SymbolMapper] = (
            None if broker_name == "auto" else SymbolMapper(broker_name)
        )
        self._stale_warn_last: dict[str, float] = {}

    # ── Connection ───────────────────────────────────────────────────────

    def connect(self) -> bool:
        if sys_platform.system() != "Windows":
            logger.warning(
                "MT5 requires Windows. Use Deriv connector on {}", sys_platform.system()
            )
            return False

        if not _MT5_AVAILABLE:
            logger.error("MetaTrader5 package not installed")
            return False

        try:
            if not mt5.initialize():
                logger.error("mt5.initialize() failed: {}", mt5.last_error())
                return False

            if self._login:
                auth = mt5.login(
                    self._login,
                    password=self._password,
                    server=self._server,
                )
                if not auth:
                    logger.error("MT5 login failed: {}", mt5.last_error())
                    mt5.shutdown()
                    return False

            info = mt5.account_info()
            self._connected = True
            logger.info(
                "MT5 connected — account {} | balance {} {}",
                mask_account_id(info.login),
                info.balance,
                info.currency,
            )

            # Auto-detect broker name from terminal and reload symbol mapper
            if self._broker_name == "auto":
                import re as _re
                term = mt5.terminal_info()
                raw = getattr(term, "company", "") if term else ""
                slug = _re.sub(r"[^a-z0-9]", "_", raw.lower()).strip("_")
                slug = _re.sub(r"_+", "_", slug) or "mt5_broker"
                self._broker_name = slug
                self._mapper = SymbolMapper(slug)
                logger.info("MT5 broker identified: '{}' → config slug: '{}'", raw, slug)

            return True
        except Exception as exc:
            logger.error("MT5 connect error: {}", exc)
            return False

    def disconnect(self) -> None:
        if _MT5_AVAILABLE and self._connected:
            try:
                mt5.shutdown()
            except Exception as exc:
                logger.warning("MT5 shutdown error: {}", exc)
        self._connected = False
        logger.info("MT5 disconnected")

    def is_connected(self) -> bool:
        if not _MT5_AVAILABLE or not self._connected:
            return False
        try:
            info = mt5.terminal_info()
            return info is not None and info.connected
        except Exception as exc:
            logger.warning("[mt5] health check failed: {}", exc)
            return False

    # ── Account ──────────────────────────────────────────────────────────

    def get_account_info(self) -> AccountInfo:
        self._require_connection()
        info = mt5.account_info()
        return AccountInfo(
            balance=info.balance,
            equity=info.equity,
            margin=info.margin,
            free_margin=info.margin_free,
            margin_level=info.margin_level or 0.0,
            currency=info.currency,
            leverage=info.leverage,
            platform="mt5",
        )

    def get_symbol_spec(self, symbol: str) -> dict:
        """Broker-truth symbol economics from MT5 ``symbol_info``.

        Exposes the exact values the system currently estimates or reads from
        config: overnight swap (``swap_long``/``swap_short``), tick value/size,
        contract size, trade mode (full / long-only / short-only / close-only /
        disabled) and volume constraints. Read-only — provided so callers can
        use broker truth instead of assumptions; wiring these into position
        sizing / the swap model is a deliberate, separately-tested change.
        Returns ``{}`` when unavailable.
        """
        self._require_connection()
        mapped = self.symbol_map(symbol)
        if mapped is None:
            return {}
        try:
            si = mt5.symbol_info(mapped)
            if si is None:
                return {}
            return {
                "symbol": symbol,
                "broker_symbol": mapped,
                "swap_long": getattr(si, "swap_long", None),
                "swap_short": getattr(si, "swap_short", None),
                "swap_mode": getattr(si, "swap_mode", None),
                "trade_tick_value": getattr(si, "trade_tick_value", None),
                "trade_tick_size": getattr(si, "trade_tick_size", None),
                "trade_contract_size": getattr(si, "trade_contract_size", None),
                "trade_mode": getattr(si, "trade_mode", None),
                "volume_min": getattr(si, "volume_min", None),
                "volume_max": getattr(si, "volume_max", None),
                "volume_step": getattr(si, "volume_step", None),
                "digits": getattr(si, "digits", None),
                "point": getattr(si, "point", None),
            }
        except Exception as exc:
            logger.warning("[mt5] get_symbol_spec failed for {}: {}", symbol, exc)
            return {}

    def get_deal_history(self, from_dt, to_dt) -> list[dict]:
        """Return the account's closed deals in [from_dt, to_dt] as plain dicts.

        Used by the broker-history ingest to reconstruct round-trip trades for
        reporting / equity reconstruction. Returns [] on any failure.
        """
        self._require_connection()
        try:
            deals = mt5.history_deals_get(from_dt, to_dt)
        except Exception as exc:
            logger.warning("[mt5] history_deals_get(range) failed: {}", exc)
            return []
        if not deals:
            return []
        out: list[dict] = []
        for d in deals:
            out.append({
                "ticket": getattr(d, "ticket", 0),
                "order": getattr(d, "order", 0),
                "position_id": getattr(d, "position_id", 0),
                "time": getattr(d, "time", 0),
                "symbol": getattr(d, "symbol", ""),
                "type": getattr(d, "type", -1),
                "entry": getattr(d, "entry", -1),
                "volume": getattr(d, "volume", 0.0),
                "price": getattr(d, "price", 0.0),
                "commission": getattr(d, "commission", 0.0),
                "swap": getattr(d, "swap", 0.0),
                "fee": getattr(d, "fee", 0.0),
                "profit": getattr(d, "profit", 0.0),
                "magic": getattr(d, "magic", 0),
                "comment": getattr(d, "comment", ""),
            })
        return out

    # ── Market data ──────────────────────────────────────────────────────

    def get_price(self, symbol: str) -> TickData:
        self._require_connection()
        mapped = self.symbol_map(symbol)
        if mapped is None:
            raise RuntimeError(
                f"Symbol '{symbol}' not available on broker '{self._broker_name}'"
            )
        tick = mt5.symbol_info_tick(mapped)
        if tick is None:
            raise RuntimeError(f"No tick data for {mapped}: {mt5.last_error()}")

        if tick.bid <= 0 or tick.ask <= 0:
            logger.warning(
                "Non-positive tick for {}: bid={}, ask={}", mapped, tick.bid, tick.ask
            )
            raise RuntimeError(
                f"Non-positive tick for {mapped}: bid={tick.bid}, ask={tick.ask}"
            )

        tick_time = datetime.fromtimestamp(tick.time, tz=timezone.utc)
        if tick.time <= 0:
            logger.warning("Invalid tick timestamp for {}: epoch={}", mapped, tick.time)
            raise RuntimeError(
                f"Invalid tick timestamp for {mapped}: epoch={tick.time}"
            )
        age = (datetime.now(timezone.utc) - tick_time).total_seconds()
        if age > self._max_tick_age_seconds:
            now_mono = _time.monotonic()
            if now_mono - self._stale_warn_last.get(mapped, 0.0) > 60.0:
                logger.warning(
                    "Stale tick for {}: {:.1f}s old (limit {}s)",
                    mapped, age, self._max_tick_age_seconds,
                )
                self._stale_warn_last[mapped] = now_mono
            raise RuntimeError(
                f"Stale tick for {mapped}: {age:.1f}s old (limit {self._max_tick_age_seconds}s)"
            )

        pip_size = get_pip_size(symbol)
        return TickData(
            bid=tick.bid,
            ask=tick.ask,
            spread=round((tick.ask - tick.bid) / pip_size, 1),
            time=tick_time,
        )

    def get_tick(self, symbol: str) -> TickData:
        return self.get_price(symbol)

    def get_ohlcv(
        self, symbol: str, timeframe: str, count: int = 200,
        *, include_forming: bool = True,
    ) -> pd.DataFrame:
        self._require_connection()
        mapped = self.symbol_map(symbol)
        # None means already confirmed not on this broker — skip immediately
        if mapped is None:
            raise RuntimeError(f"Symbol '{symbol}' not available on broker '{self._broker_name}'")
        tf_const = self.timeframe_map(timeframe)
        rates = mt5.copy_rates_from_pos(mapped, tf_const, 0, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(
                f"No OHLCV for {mapped}/{timeframe}: {mt5.last_error()}"
            )
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.rename(
            columns={"tick_volume": "volume", "real_volume": "real_volume"}
        )
        out = df[["time", "open", "high", "low", "close", "volume"]]
        # MT5 copy_rates_from_pos(..., 0, count) returns the in-progress bar as
        # the last row. Callers that need confirmed-closed data can request the
        # forming bar dropped here (default keeps it for the established
        # closed-bar dual-view: structural modules drop it themselves, live
        # observational reads legitimately keep it).
        if not include_forming and len(out) > 1:
            out = out.iloc[:-1]
        return out

    def get_spread(self, symbol: str) -> float:
        return self.get_price(symbol).spread

    def _get_symbol_constraints(self, broker_symbol: str) -> dict:
        """
        Read symbol constraints from broker JSON, with in-memory cache.
        Falls back to empty dict — place_order() then fetches live via symbol_info().
        """
        # In-memory cache — avoids reading JSON on every order
        if not hasattr(self, "_constraints_cache"):
            self._constraints_cache: dict = {}

        if broker_symbol in self._constraints_cache:
            return self._constraints_cache[broker_symbol]

        try:
            import json as _json
            from pathlib import Path
            cfg_path = Path(__file__).parent.parent.parent / "config" / "brokers" / f"{self._broker_name}.json"
            if not cfg_path.exists():
                return {}
            with open(cfg_path) as f:
                cfg = _json.load(f)
            all_constraints = cfg.get("symbol_constraints", {})
            # Cache entire file contents for this session
            self._constraints_cache.update(all_constraints)
            return self._constraints_cache.get(broker_symbol, {})
        except Exception as exc:
            logger.warning("[mt5] symbol constraints load failed: {}", exc)
            return {}

    # ── Order execution ──────────────────────────────────────────────────

    def _deviation_points(self, symbol: str, point: float) -> int:
        """Per-instrument max-slippage cap in broker points.

        Converts the configured max-slippage (pips) into this symbol's point
        units so the cap is consistent across FX / metals / indices / crypto.
        Falls back to the legacy global deviation when max_slippage_pips is not
        configured (0) or the point size is unknown.
        """
        if self._max_slippage_pips > 0 and point and point > 0:
            pip_size = get_pip_size(symbol)
            pts = int(round(self._max_slippage_pips * pip_size / point))
            return max(1, pts)
        return self._deviation

    @staticmethod
    def _restore_tp_for_rr(
        price: float,
        sl: float,
        tp: float,
        is_buy: bool,
        orig_rr: float,
        digits: int,
    ) -> float:
        """Return a TP that restores ``orig_rr`` against the (widened) SL.

        Extends the take-profit outward to ``adjusted_risk × orig_rr`` so a
        broker minimum-stop adjustment that widened the SL cannot collapse the
        pipeline-approved reward:risk. The TP is only ever pushed FURTHER from
        price (more reward, never added risk); if the recomputed TP would be
        nearer than the current one, the current TP is kept unchanged.
        """
        if orig_rr <= 0 or tp <= 0:
            return tp
        adj_risk = abs(price - sl)
        if adj_risk <= 0:
            return tp
        target_reward = adj_risk * orig_rr
        restored_tp = (price + target_reward) if is_buy else (price - target_reward)
        restored_tp = round(restored_tp, digits)
        if (is_buy and restored_tp > tp) or (not is_buy and restored_tp < tp):
            return restored_tp
        return tp

    def place_order(
        self,
        symbol: str,
        direction: str,
        lots: float,
        sl: float,
        tp: float,
        comment: str = "",
        idempotency_key: str = "",
    ) -> OrderResult:
        self._require_connection()
        mapped = self.symbol_map(symbol)
        if mapped is None:
            return OrderResult(
                success=False, order_id="", fill_price=0.0,
                requested_price=0.0, slippage_pips=0.0, lots=lots,
                symbol=symbol, direction=str(direction).upper(),
                sl=sl, tp=tp, platform="mt5",
                error=f"Symbol '{symbol}' not available on broker '{self._broker_name}'",
            )

        if idempotency_key:
            dup = self._find_order_by_idem_key(idempotency_key)
            if dup is not None:
                logger.warning(
                    "MT5 duplicate prevented — idem_key {} already filled as ticket {}",
                    idempotency_key, dup.ticket,
                )
                return OrderResult(
                    success=True,
                    order_id=str(dup.ticket),
                    fill_price=dup.price,
                    requested_price=dup.price,
                    slippage_pips=0.0,
                    lots=dup.volume,
                    symbol=symbol,
                    direction=direction.upper(),
                    sl=dup.sl,
                    tp=dup.tp,
                    platform="mt5",
                )

        tick = mt5.symbol_info_tick(mapped)
        if tick is None:
            return self._fail_order(symbol, direction, lots, sl, tp, "No tick data")

        # Fail-closed on bad/stale ticks — never open a market position on a
        # non-positive or stale price. The staleness guard previously lived only
        # in get_price(), so market entries could fire on a frozen feed.
        if tick.bid <= 0 or tick.ask <= 0 or tick.time <= 0:
            return self._fail_order(
                symbol, direction, lots, sl, tp,
                f"Invalid tick (bid={tick.bid}, ask={tick.ask}, epoch={tick.time})",
            )
        tick_age = (
            datetime.now(timezone.utc)
            - datetime.fromtimestamp(tick.time, tz=timezone.utc)
        ).total_seconds()
        if tick_age > self._max_tick_age_seconds:
            return self._fail_order(
                symbol, direction, lots, sl, tp,
                f"STALE_TICK: {tick_age:.1f}s old (limit {self._max_tick_age_seconds}s)",
            )

        is_buy = direction.upper() in ("BUY", "LONG")
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        price = tick.ask if is_buy else tick.bid

        order_comment = str(comment or "APEX")[:31]
        # Ensure the idempotency key is embedded in the comment so dedup
        # (_find_order_by_idem_key) can match it on retries/reconnects. If the
        # caller's comment doesn't already carry the key, rebuild it.
        if idempotency_key and extract_idempotency_key(order_comment) != idempotency_key:
            order_comment = build_order_comment("APEX", idempotency_key)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": mapped,
            "volume": float(lots),
            "type": order_type,
            "price": price,
            "sl": float(sl),
            "tp": float(tp),
            "deviation": self._deviation,
            "magic": self._magic,
            "comment": order_comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        # ── Broker symbol constraints (volume + stops) ─────────────────────
        # Prefer constraints cached in broker config (discovered at startup).
        # Fall back to live symbol_info query only if not yet cached.
        cached = self._get_symbol_constraints(mapped)
        if cached:
            vol_min     = cached.get("volume_min", 0.01)
            vol_max     = cached.get("volume_max", 100.0)
            vol_step    = cached.get("volume_step", 0.01)
            stops_level = cached.get("stops_level", 0)
            digits      = cached.get("digits", 5)
            point       = cached.get("point", 0.00001)
        else:
            # Always select symbol first — ensures it's visible in Market Watch
            # so symbol_info() can return data (especially for crypto/exotic pairs)
            mt5.symbol_select(mapped, True)
            sym_info = mt5.symbol_info(mapped)
            if sym_info is not None:
                vol_min     = sym_info.volume_min
                vol_max     = sym_info.volume_max
                vol_step    = sym_info.volume_step
                stops_level = sym_info.trade_stops_level
                digits      = sym_info.digits
                point       = sym_info.point
                logger.debug(
                    "Live constraints for {} — stops={} digits={} point={} vol_min={}",
                    mapped, stops_level, digits, point, vol_min,
                )
            else:
                logger.warning("Could not fetch constraints for {} — using safe defaults", mapped)
                vol_min, vol_max, vol_step = 0.01, 100.0, 0.01
                stops_level, digits, point = 0, 5, 0.00001

        # Volume: clamp and round to broker's volume_min/max/step
        requested_lots = lots
        if vol_step > 0:
            lots = round(round(lots / vol_step) * vol_step, 10)
        lots = max(vol_min, min(vol_max, lots))
        lots = round(lots, 2)

        if lots > requested_lots and vol_min > requested_lots:
            inflation_factor = round(lots / requested_lots, 1) if requested_lots > 0 else 0.0
            logger.warning(
                "[MINLOT_INFLATION] {} — requested {:.4f} lots, broker vol_min={:.4f}, "
                "final {:.4f} lots ({}x inflation)",
                mapped, requested_lots, vol_min, lots, inflation_factor,
            )
            if self._reject_on_minlot_inflation:
                return self._fail_order(
                    symbol, direction, lots, sl, tp,
                    f"min-lot inflation: requested {requested_lots} < broker min {vol_min}",
                )

        request["volume"] = float(lots)

        # Per-instrument slippage cap: the configured max-slippage (pips) →
        # broker points using THIS symbol's point size, so 20 points doesn't
        # mean 2 pips on FX and something else on indices/gold. The broker
        # rejects/requotes fills beyond this; the retry loop re-prices.
        request["deviation"] = self._deviation_points(symbol, point)

        # Stops: enforce minimum SL/TP distance from entry price
        min_distance = self._effective_min_stop_distance(mapped, point, stops_level)
        if min_distance > 0:
            # Capture the originally-approved R:R before any broker adjustment,
            # so we can restore it if the minimum-stop floor widens the SL.
            orig_risk = abs(price - sl)
            orig_reward = abs(tp - price)
            orig_rr = (orig_reward / orig_risk) if orig_risk > 0 else 0.0
            sl_distance = abs(price - sl)
            tp_distance = abs(tp - price)
            if sl_distance < min_distance:
                new_sl = (price - min_distance) if is_buy else (price + min_distance)
                logger.warning(
                    "SL too close for {} (min {:.5f}, got {:.5f}) — adjusting to {:.5f}",
                    mapped, min_distance, sl_distance, new_sl,
                )
                sl = round(new_sl, digits)
                request["sl"] = sl
            if tp_distance > 0 and tp_distance < min_distance:
                new_tp = (price + min_distance) if is_buy else (price - min_distance)
                logger.warning(
                    "TP too close for {} (min {:.5f}, got {:.5f}) — adjusting to {:.5f}",
                    mapped, min_distance, tp_distance, new_tp,
                )
                tp = round(new_tp, digits)
                request["tp"] = tp

            # Restore the approved R:R after the SL was widened by the broker
            # floor: extend the TP outward to ``adjusted_risk × orig_rr``. This
            # only ever increases reward (never moves TP closer / adds risk) and
            # keeps a tick-size rounding from collapsing a pipeline-approved R:R.
            if self._preserve_rr_after_adjust and orig_rr > 0 and tp > 0:
                restored_tp = self._restore_tp_for_rr(
                    price, sl, tp, is_buy, orig_rr, digits
                )
                if restored_tp != tp:
                    logger.info(
                        "[MT5] {} {} — restoring approved R:R {:.2f} after SL "
                        "widening: TP {:.5f} → {:.5f}",
                        mapped, direction, orig_rr, tp, restored_tp,
                    )
                    tp = restored_tp
                    request["tp"] = tp

            # R:R viability after stop adjustment. The position sizer approved
            # the trade on the ORIGINAL (tighter) SL; once the broker minimum
            # widens it, the reward:risk that justified the entry may be gone.
            # Reject rather than silently take a degraded trade.
            if self._min_rr_after_adjust > 0 and tp > 0:
                adj_risk = abs(price - sl)
                adj_reward = abs(tp - price)
                if adj_risk > 0:
                    adj_rr = adj_reward / adj_risk
                    if adj_rr < self._min_rr_after_adjust:
                        logger.warning(
                            "[MT5] Order rejected — SL/TP adjustment destroyed R:R "
                            "(new R:R={:.2f}, minimum={:.2f}) for {} {}",
                            adj_rr, self._min_rr_after_adjust, mapped, direction,
                        )
                        return self._fail_order(
                            symbol, direction, lots, sl, tp,
                            f"RR_TOO_LOW_AFTER_ADJUST: new R:R {adj_rr:.2f} < "
                            f"min {self._min_rr_after_adjust:.2f}",
                        )

        # ── Exact pre-trade margin check (broker's own math) ───────────────
        # Ask the broker exactly how much margin this order needs and fail
        # CLOSED if it exceeds free margin — prevents margin-call entries that a
        # balance/equity estimate would miss. order_calc_margin returns None if
        # it cannot compute (e.g. unsupported), in which case we skip the check.
        try:
            req_margin = mt5.order_calc_margin(order_type, mapped, float(lots), price)
            acct = mt5.account_info()
            free_margin = float(getattr(acct, "margin_free", 0.0) or 0.0) if acct else 0.0
            if req_margin is not None and req_margin > 0 and free_margin > 0 and req_margin > free_margin:
                logger.error(
                    "MT5 INSUFFICIENT MARGIN — {} {} {} lots needs ${:.2f} > free ${:.2f}; refusing",
                    direction, mapped, lots, req_margin, free_margin,
                )
                return self._fail_order(
                    symbol, direction, lots, sl, tp,
                    f"INSUFFICIENT_MARGIN: needs ${req_margin:.2f} > free ${free_margin:.2f}",
                )
        except Exception as exc:
            logger.debug("[mt5] order_calc_margin pre-check skipped: {}", exc)

        # Retcodes that mean "a position exists". DONE_PARTIAL is a real (smaller)
        # fill, not a failure — we read result.volume below to size accordingly.
        done_codes = {
            mt5.TRADE_RETCODE_DONE,
            getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010),
        }
        # Transient codes worth re-pricing + retrying. Permanent codes
        # (INVALID_STOPS, NO_MONEY, market closed) are NOT retried.
        retryable_codes = {
            getattr(mt5, "TRADE_RETCODE_REQUOTE", 10004),
            getattr(mt5, "TRADE_RETCODE_PRICE_CHANGED", 10020),
            getattr(mt5, "TRADE_RETCODE_PRICE_OFF", 10021),
            getattr(mt5, "TRADE_RETCODE_TIMEOUT", 10012),
            getattr(mt5, "TRADE_RETCODE_CONNECTION", 10031),
        }

        max_attempts = 3
        result = None
        latency = 0.0
        for attempt in range(1, max_attempts + 1):
            # On a retry, the prior send may have actually filled despite an
            # ambiguous TIMEOUT/CONNECTION error. Re-check idempotency before
            # re-sending to avoid a duplicate position.
            if attempt > 1 and idempotency_key:
                dup = self._find_order_by_idem_key(idempotency_key)
                if dup is not None:
                    logger.warning(
                        "MT5 retry duplicate prevented — idem_key {} already filled as ticket {}",
                        idempotency_key, dup.ticket,
                    )
                    return OrderResult(
                        success=True,
                        order_id=str(dup.ticket),
                        fill_price=dup.price,
                        requested_price=dup.price,
                        slippage_pips=0.0,
                        lots=dup.volume,
                        symbol=symbol,
                        direction=direction.upper(),
                        sl=dup.sl,
                        tp=dup.tp,
                        platform="mt5",
                    )
            t0 = _time.monotonic()
            result = mt5.order_send(request)
            latency = (_time.monotonic() - t0) * 1000

            if result is not None and result.retcode in done_codes:
                break

            err = result.comment if result else str(mt5.last_error())
            retcode = result.retcode if result else None

            # Tag market-closed errors so the execution circuit breaker upstream
            # does NOT count them as real failures — a closed market is expected.
            if "market closed" in err.lower() or "market is closed" in err.lower():
                return self._fail_order(symbol, direction, lots, sl, tp, f"MARKET_CLOSED: {err}")

            if retcode in retryable_codes and attempt < max_attempts:
                logger.warning(
                    "MT5 order transient failure ({}) — {} {}, attempt {}/{}, re-pricing",
                    err, direction, mapped, attempt, max_attempts,
                )
                # Re-price off a fresh tick before retrying (requote/price-off).
                fresh = mt5.symbol_info_tick(mapped)
                if fresh is not None and fresh.bid > 0 and fresh.ask > 0:
                    price = fresh.ask if is_buy else fresh.bid
                    request["price"] = price
                continue

            logger.error("MT5 order failed — {} {} {} lots: {}", direction, mapped, lots, err)
            return self._fail_order(symbol, direction, lots, sl, tp, err)

        if result is None or result.retcode not in done_codes:
            err = result.comment if result else str(mt5.last_error())
            logger.error(
                "MT5 order failed after {} attempts — {} {}: {}",
                max_attempts, direction, mapped, err,
            )
            return self._fail_order(symbol, direction, lots, sl, tp, err)

        # Use the broker's actually-filled volume (handles partial fills) so the
        # managed position is seeded with the real size, not the requested size.
        filled_lots = float(getattr(result, "volume", 0.0) or 0.0)
        if filled_lots <= 0:
            filled_lots = lots
        if abs(filled_lots - lots) > 1e-9:
            logger.warning(
                "MT5 PARTIAL/ADJUSTED FILL — {} {} requested {:.2f} lots, filled {:.2f} lots",
                direction, mapped, lots, filled_lots,
            )

        pip_size = get_pip_size(resolve_to_internal(symbol))
        slippage = abs(result.price - price) / pip_size if pip_size > 0 else 0.0

        # Loud backstop: the deviation cap should have rejected/requoted a fill
        # beyond tolerance, but market-execution accounts may ignore deviation.
        # The order's SL/TP are absolute price levels already submitted to the
        # broker, so they remain enforced server-side regardless of slippage.
        # We surface severe slippage at ERROR so monitoring catches a degraded
        # entry R:R; a normal overshoot stays at WARNING.
        if self._max_slippage_pips > 0 and slippage > self._max_slippage_pips + 1e-9:
            severe = slippage > (3.0 * self._max_slippage_pips)
            log = logger.error if severe else logger.warning
            log(
                "{} SLIPPAGE EXCEEDED — {} {} filled {:.1f}pip beyond plan "
                "(cap {:.1f}pip); broker may not honour deviation on this account",
                "🚨" if severe else "⚠️",
                direction, mapped, slippage, self._max_slippage_pips,
            )

        logger.info(
            "MT5 order filled — {} {} {} lots @ {} (slip {:.1f} pip, {:.0f}ms)",
            direction, mapped, filled_lots, result.price, slippage, latency,
        )
        return OrderResult(
            success=True,
            order_id=str(result.order),
            fill_price=result.price,
            requested_price=price,
            slippage_pips=round(slippage, 2),
            lots=filled_lots,
            symbol=symbol,
            direction=direction.upper(),
            sl=sl,
            tp=tp,
            platform="mt5",
        )

    _PENDING_TYPE_MAP = {
        "BUY_LIMIT": "ORDER_TYPE_BUY_LIMIT",
        "SELL_LIMIT": "ORDER_TYPE_SELL_LIMIT",
        "BUY_STOP": "ORDER_TYPE_BUY_STOP",
        "SELL_STOP": "ORDER_TYPE_SELL_STOP",
    }

    def place_pending_order(
        self,
        symbol: str,
        order_kind: str,
        entry_price: float,
        lots: float,
        sl: float,
        tp: float,
        comment: str = "",
        idempotency_key: str = "",
    ) -> OrderResult:
        self._require_connection()
        mapped = self.symbol_map(symbol)
        if mapped is None:
            return OrderResult(
                success=False, order_id="", fill_price=0.0,
                requested_price=entry_price, slippage_pips=0.0, lots=lots,
                symbol=symbol,
                direction="BUY" if str(order_kind).upper().startswith("BUY") else "SELL",
                sl=sl, tp=tp, platform="mt5",
                error=f"Symbol '{symbol}' not available on broker '{self._broker_name}'",
            )

        if idempotency_key:
            dup = self._find_order_by_idem_key(idempotency_key)
            if dup is not None:
                logger.warning(
                    "MT5 pending duplicate prevented — idem_key {} already exists as ticket {}",
                    idempotency_key, dup.ticket,
                )
                return OrderResult(
                    success=True,
                    order_id=str(dup.ticket),
                    fill_price=dup.price,
                    requested_price=entry_price,
                    slippage_pips=0.0,
                    lots=dup.volume,
                    symbol=symbol,
                    direction=order_kind.split("_")[0],
                    sl=dup.sl,
                    tp=dup.tp,
                    platform="mt5",
                )

        mt5_type_name = self._PENDING_TYPE_MAP.get(order_kind.upper())
        if mt5_type_name is None:
            return self._fail_order(symbol, order_kind, lots, sl, tp, f"Unknown pending type: {order_kind}")
        mt5_order_type = getattr(mt5, mt5_type_name, None)
        if mt5_order_type is None:
            return self._fail_order(symbol, order_kind, lots, sl, tp, f"MT5 constant not found: {mt5_type_name}")

        cached = self._get_symbol_constraints(mapped)
        if cached:
            vol_min = cached.get("volume_min", 0.01)
            vol_max = cached.get("volume_max", 100.0)
            vol_step = cached.get("volume_step", 0.01)
            digits = cached.get("digits", 5)
        else:
            mt5.symbol_select(mapped, True)
            sym_info = mt5.symbol_info(mapped)
            if sym_info is not None:
                vol_min, vol_max, vol_step = sym_info.volume_min, sym_info.volume_max, sym_info.volume_step
                digits = sym_info.digits
            else:
                vol_min, vol_max, vol_step, digits = 0.01, 100.0, 0.01, 5

        requested_lots = lots
        if vol_step > 0:
            lots = round(round(lots / vol_step) * vol_step, 10)
        lots = max(vol_min, min(vol_max, lots))
        lots = round(lots, 2)

        if lots > requested_lots and vol_min > requested_lots:
            inflation_factor = round(lots / requested_lots, 1) if requested_lots > 0 else 0.0
            logger.warning(
                "[MINLOT_INFLATION] {} — requested {:.4f} lots, broker vol_min={:.4f}, "
                "final {:.4f} lots ({}x inflation)",
                mapped, requested_lots, vol_min, lots, inflation_factor,
            )
            if self._reject_on_minlot_inflation:
                return self._fail_order(
                    symbol, order_kind, lots, sl, tp,
                    f"min-lot inflation: requested {requested_lots} < broker min {vol_min}",
                )

        pending_comment = comment or "APEX_PENDING"

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": mapped,
            "volume": float(lots),
            "type": mt5_order_type,
            "price": round(entry_price, digits),
            "sl": float(sl),
            "tp": float(tp),
            "deviation": self._deviation,
            "magic": self._magic,
            "comment": pending_comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(mt5.last_error())
            logger.error("MT5 pending order failed — {} {} {} lots @ {}: {}", order_kind, mapped, lots, entry_price, err)
            return self._fail_order(symbol, order_kind, lots, sl, tp, err)

        logger.info(
            "MT5 pending placed — {} {} {} lots @ {}",
            order_kind, mapped, lots, entry_price,
        )
        return OrderResult(
            success=True,
            order_id=str(result.order),
            fill_price=entry_price,
            requested_price=entry_price,
            slippage_pips=0.0,
            lots=lots,
            symbol=symbol,
            direction=order_kind.split("_")[0],
            sl=sl,
            tp=tp,
            platform="mt5",
        )

    def cancel_pending_order(self, order_id: str) -> bool:
        """Delete a resting pending (limit/stop) order by its ticket.

        Uses ``TRADE_ACTION_REMOVE`` to remove the order from the book. Returns
        True on success, False on any failure (invalid ticket, broker reject,
        no connection). Never raises — a failed cancel is reported, not thrown.
        """
        try:
            self._require_connection()
        except Exception as exc:  # noqa: BLE001 — surface as a failed cancel
            logger.error("MT5 cancel pending — not connected: {}", exc)
            return False
        try:
            ticket = int(order_id)
        except (TypeError, ValueError):
            logger.error("MT5 cancel pending — invalid ticket {!r}", order_id)
            return False

        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": ticket,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(mt5.last_error())
            logger.error("MT5 cancel pending failed — ticket {}: {}", ticket, err)
            return False
        logger.info("MT5 pending cancelled — ticket {}", ticket)
        return True

    def _effective_min_stop_distance(
        self,
        broker_symbol: str,
        point: float,
        stops_level: int,
        freeze_level: int = 0,
    ) -> float:
        """Minimum allowed stop distance from price, in price units.

        Combines the broker-declared stops/freeze levels with a spread-based
        floor. The spread floor is the key fix for symbols that report
        stops_level == 0 (e.g. BTCUSD) but still reject tight stops.
        """
        min_distance = max(int(stops_level or 0), int(freeze_level or 0)) * point
        try:
            tick = mt5.symbol_info_tick(broker_symbol)
            if tick is not None and tick.ask > 0 and tick.bid > 0:
                spread = tick.ask - tick.bid
                if spread > 0:
                    min_distance = max(min_distance, spread * _STOP_SPREAD_FLOOR_MULT)
        except Exception as exc:
            logger.debug("[mt5] spread-floor lookup failed for {}: {}", broker_symbol, exc)
        return min_distance

    def _clamp_stop_distance(
        self,
        broker_symbol: str,
        price: float,
        sl: Optional[float],
        tp: Optional[float],
        is_buy: bool,
    ) -> tuple[Optional[float], Optional[float], float]:
        """Clamp SL/TP to the broker minimum stop distance.

        Returns (clamped_sl, clamped_tp, min_distance).
        """
        cached = self._get_symbol_constraints(broker_symbol)
        if cached:
            stops_level = cached.get("stops_level", 0)
            digits = cached.get("digits", 5)
            point = cached.get("point", 0.00001)
            freeze_level = cached.get("freeze_level", 0)
        else:
            mt5.symbol_select(broker_symbol, True)
            sym_info = mt5.symbol_info(broker_symbol)
            if sym_info is not None:
                stops_level = sym_info.trade_stops_level
                digits = sym_info.digits
                point = sym_info.point
                freeze_level = getattr(sym_info, "trade_freeze_level", 0)
            else:
                stops_level, digits, point, freeze_level = 0, 5, 0.00001, 0

        min_distance = self._effective_min_stop_distance(
            broker_symbol, point, stops_level, freeze_level,
        )

        clamped_sl = sl
        clamped_tp = tp
        if min_distance > 0:
            if sl is not None and abs(price - sl) < min_distance:
                clamped_sl = round(
                    (price - min_distance) if is_buy else (price + min_distance),
                    digits,
                )
                logger.warning(
                    "SL too close on modify for {} (min {:.5f}, got {:.5f}) — clamped to {:.5f}",
                    broker_symbol, min_distance, abs(price - sl), clamped_sl,
                )
            if tp is not None and abs(tp - price) > 0 and abs(tp - price) < min_distance:
                clamped_tp = round(
                    (price + min_distance) if is_buy else (price - min_distance),
                    digits,
                )
                logger.warning(
                    "TP too close on modify for {} (min {:.5f}, got {:.5f}) — clamped to {:.5f}",
                    broker_symbol, min_distance, abs(tp - price), clamped_tp,
                )
        return clamped_sl, clamped_tp, min_distance

    def modify_order(
        self,
        order_id: str,
        new_sl: Optional[float] = None,
        new_tp: Optional[float] = None,
    ) -> bool:
        self._require_connection()
        position = self._find_position(order_id)
        if position is None:
            logger.error("MT5 modify — position {} not found", order_id)
            return False

        is_buy = position.type == mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(position.symbol)
        if tick is None:
            logger.error("MT5 modify — no tick for {} (position {})", position.symbol, order_id)
            return False
        current_price = tick.bid if is_buy else tick.ask

        raw_sl = float(new_sl) if new_sl is not None else position.sl
        raw_tp = float(new_tp) if new_tp is not None else position.tp

        clamped_sl, clamped_tp, _ = self._clamp_stop_distance(
            position.symbol, current_price,
            raw_sl if new_sl is not None else None,
            raw_tp if new_tp is not None else None,
            is_buy,
        )
        final_sl = clamped_sl if new_sl is not None else raw_sl
        final_tp = clamped_tp if new_tp is not None else raw_tp

        # Safety: the broker min-distance clamp must never LOOSEN an existing
        # protective stop (that would increase risk). If clamping pushed the new
        # SL to the wrong side of the current stop, keep the current stop.
        if new_sl is not None and position.sl and position.sl > 0:
            loosened = (
                (is_buy and final_sl < position.sl)
                or (not is_buy and final_sl > position.sl)
            )
            if loosened:
                logger.warning(
                    "MT5 modify — clamped SL {:.5f} would loosen existing {:.5f} for {} "
                    "(broker min-distance); keeping current SL",
                    final_sl, position.sl, position.symbol,
                )
                final_sl = position.sl

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": int(order_id),
            "symbol": position.symbol,
            "sl": float(final_sl),
            "tp": float(final_tp),
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(mt5.last_error())
            logger.error("MT5 modify failed for {}: {}", order_id, err)
            return False
        logger.info("MT5 modified {} — SL={} TP={}", order_id, request["sl"], request["tp"])
        return True

    def close_order(
        self, order_id: str, lots: Optional[float] = None
    ) -> CloseResult:
        self._require_connection()
        position = self._find_position(order_id)
        if position is None:
            return CloseResult(
                success=False,
                order_id=order_id,
                close_price=0.0,
                lots_closed=0.0,
                pnl=0.0,
                platform="mt5",
                error="Position not found",
            )

        is_buy = position.type == mt5.ORDER_TYPE_BUY
        close_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(position.symbol)
        if tick is None or (tick.bid == 0.0 and tick.ask == 0.0):
            # No live tick — market is closed or symbol disabled. Tag it so the
            # executor circuit breaker does NOT treat this as a real failure.
            err = f"MARKET_CLOSED: no tick for {position.symbol}"
            logger.warning("MT5 close skipped for {}: {}", order_id, err)
            return CloseResult(
                success=False,
                order_id=order_id,
                close_price=0.0,
                lots_closed=0.0,
                pnl=0.0,
                platform="mt5",
                error=err,
            )
        price = tick.bid if is_buy else tick.ask
        close_lots = lots if lots is not None else position.volume

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": float(close_lots),
            "type": close_type,
            "position": int(order_id),
            "price": price,
            "deviation": self._deviation,
            "magic": self._magic,
            "comment": "APEX_CLOSE",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(mt5.last_error())
            # Tag market-closed closes so the executor circuit breaker does not
            # count an expected weekend/holiday close as a real broker failure.
            if "market closed" in err.lower() or "market is closed" in err.lower():
                err = f"MARKET_CLOSED: {err}"
            logger.error("MT5 close failed for {}: {}", order_id, err)
            return CloseResult(
                success=False,
                order_id=order_id,
                close_price=0.0,
                lots_closed=0.0,
                pnl=0.0,
                platform="mt5",
                error=err,
            )

        logger.info("MT5 closed {} — {} lots @ {}", order_id, close_lots, result.price)
        return CloseResult(
            success=True,
            order_id=order_id,
            close_price=result.price,
            lots_closed=close_lots,
            pnl=position.profit,
            platform="mt5",
        )

    # ── Positions ────────────────────────────────────────────────────────

    def get_open_positions(self) -> list[PositionInfo]:
        self._require_connection()
        positions = mt5.positions_get()
        if positions is None:
            return []
        return [self._to_position_info(p) for p in positions]

    def get_position_info(self, order_id: str) -> Optional[PositionInfo]:
        self._require_connection()
        position = self._find_position(order_id)
        if position is None:
            return None
        return self._to_position_info(position)

    def get_realized_pnl(self, order_id: str) -> Optional[float]:
        """Return the broker's realized P&L for a closed ticket via history_deals_get.

        Superseded for the event-driven close path by ``get_deal_close_info()``,
        which returns the same net P&L plus exit attribution and close price.
        Kept for backward compatibility with older callers.
        Returns None if the deal history isn't available.
        """
        self._require_connection()
        try:
            ticket = int(order_id)
            deals = mt5.history_deals_get(position=ticket)
            if deals is None or len(deals) == 0:
                return None
            total_pnl = sum(d.profit + d.commission + d.swap + d.fee for d in deals)
            return round(total_pnl, 2)
        except Exception as exc:
            logger.warning("[mt5] PnL history deals fetch failed for ticket: {}", exc)
            return None

    def get_deal_close_info(self, order_id: str) -> Optional[DealCloseInfo]:
        """Return structured close details from MT5 deal history."""
        self._require_connection()
        try:
            ticket = int(order_id)
            deals = mt5.history_deals_get(position=ticket)
            if deals is None or len(deals) == 0:
                return None
            total_commission = float(
                sum(getattr(d, "commission", 0.0) or 0.0 for d in deals),
            )
            total_swap = float(
                sum(getattr(d, "swap", 0.0) or 0.0 for d in deals),
            )
            total_fee = float(
                sum(getattr(d, "fee", 0.0) or 0.0 for d in deals),
            )
            total_profit = float(
                sum(getattr(d, "profit", 0.0) or 0.0 for d in deals),
            )
            total_pnl = total_profit + total_commission + total_swap + total_fee
            closing_deal = None
            for d in deals:
                if getattr(d, "entry", None) == 1:  # DEAL_ENTRY_OUT
                    closing_deal = d
                    break
            if closing_deal is None:
                closing_deal = deals[-1]
            reason_code = getattr(closing_deal, "reason", None)
            comment = getattr(closing_deal, "comment", None)
            exit_reason = _MT5_DEAL_REASON_MAP.get(reason_code, "BROKER_CLOSED_UNKNOWN")
            fill_price = getattr(closing_deal, "price", None)
            close_time_epoch = getattr(closing_deal, "time", None)
            close_time = (
                datetime.fromtimestamp(close_time_epoch, tz=timezone.utc)
                if close_time_epoch
                else None
            )
            return DealCloseInfo(
                pnl=round(total_pnl, 2),
                exit_reason=exit_reason,
                commission=round(total_commission, 2),
                swap=round(total_swap, 2),
                fee=round(total_fee, 2),
                raw_reason_code=reason_code,
                raw_comment=comment or "",
                close_price=fill_price,
                close_time=close_time,
            )
        except Exception as exc:
            logger.warning("[mt5] Deal close info fetch failed for ticket: {}", exc)
            return None

    # ── Symbol / timeframe mapping ───────────────────────────────────────

    def symbol_map(self, apex_symbol: str) -> str:
        if apex_symbol in self._symbol_cache:
            return self._symbol_cache[apex_symbol]

        if self._mapper is None:
            # Mapper not ready yet — connect() not called yet, passthrough
            mapped = apex_symbol
        else:
            mapped = self._mapper.to_broker(apex_symbol)
        if mapped != apex_symbol:
            if _MT5_AVAILABLE and self._connected:
                info = mt5.symbol_info(mapped)
                if info is not None:
                    if not info.visible:
                        mt5.symbol_select(mapped, True)
                    self._symbol_cache[apex_symbol] = mapped
                    return mapped
            else:
                self._symbol_cache[apex_symbol] = mapped
                return mapped

        if not _MT5_AVAILABLE or not self._connected:
            return apex_symbol

        for suffix in _COMMON_SUFFIXES:
            candidate = apex_symbol + suffix
            info = mt5.symbol_info(candidate)
            if info is not None:
                if not info.visible:
                    mt5.symbol_select(candidate, True)
                self._symbol_cache[apex_symbol] = candidate
                return candidate

        # Try broker-agnostic fallback aliases for indices/commodities
        for alias in _FALLBACK_ALIASES.get(apex_symbol, []):
            info = mt5.symbol_info(alias)
            if info is not None:
                if not info.visible:
                    mt5.symbol_select(alias, True)
                logger.info(
                    "Symbol fallback: {} → {} (add to broker JSON to avoid this scan)",
                    apex_symbol, alias,
                )
                self._symbol_cache[apex_symbol] = alias
                return alias

        # Nothing found — log once and skip gracefully
        logger.warning(
            "Symbol {} not found on broker. Run scripts/discover_symbols.py "
            "to find the correct name and add it to config/brokers/YOUR_BROKER.json",
            apex_symbol,
        )
        # Confirmed not on this broker — cache None, warn once, skip silently after
        if apex_symbol not in self._not_found_warned:
            logger.warning(
                "Symbol '{}' not found on broker '{}' — will be skipped silently from now on. "
                "Run scripts/discover_symbols.py to find the correct name.",
                apex_symbol, self._broker_name,
            )
            self._not_found_warned.add(apex_symbol)
        self._symbol_cache[apex_symbol] = None
        return None

    def timeframe_map(self, tf: str) -> Any:
        if not _MT5_AVAILABLE:
            return tf
        return _TF_MAP.get(tf.upper(), mt5.TIMEFRAME_H1)

    # ── Private helpers ──────────────────────────────────────────────────

    def _require_connection(self) -> None:
        if not self._connected:
            raise ConnectionError("MT5 is not connected")

    def _find_position(self, order_id: str) -> Any:
        positions = mt5.positions_get(ticket=int(order_id))
        if positions and len(positions) > 0:
            return positions[0]
        return None

    def _find_order_by_idem_key(self, idem_key: str) -> Optional[SimpleNamespace]:
        """Check positions, pending orders, and recent history for an existing idem key.

        Returns a normalised SimpleNamespace(ticket, price, volume, sl, tp)
        so every consumer sees the same shape regardless of source.
        """
        positions = mt5.positions_get()
        if positions:
            for p in positions:
                if idem_key in (getattr(p, "comment", "") or ""):
                    return SimpleNamespace(
                        ticket=p.ticket,
                        price=p.price_open,
                        volume=p.volume,
                        sl=p.sl,
                        tp=p.tp,
                    )
        orders = mt5.orders_get()
        if orders:
            for o in orders:
                if idem_key in (getattr(o, "comment", "") or ""):
                    return SimpleNamespace(
                        ticket=o.ticket,
                        price=getattr(o, "price_open", 0.0),
                        volume=getattr(o, "volume_current", getattr(o, "volume_initial", 0.0)),
                        sl=getattr(o, "sl", 0.0),
                        tp=getattr(o, "tp", 0.0),
                    )
        try:
            since = datetime.now(timezone.utc) - timedelta(seconds=900)
            now = datetime.now(timezone.utc)
            deals = mt5.history_deals_get(since, now)
            if deals:
                for d in deals:
                    if idem_key in (getattr(d, "comment", "") or ""):
                        return SimpleNamespace(
                            ticket=d.ticket,
                            price=getattr(d, "price", 0.0),
                            volume=getattr(d, "volume", 0.0),
                            sl=0.0,
                            tp=0.0,
                        )
        except Exception as exc:
            logger.warning("MT5 history_deals_get failed during dedup: {}", exc)
        try:
            since = datetime.now(timezone.utc) - timedelta(seconds=900)
            now = datetime.now(timezone.utc)
            hist_orders = mt5.history_orders_get(since, now)
            if hist_orders:
                for ho in hist_orders:
                    if idem_key in (getattr(ho, "comment", "") or ""):
                        return SimpleNamespace(
                            ticket=ho.ticket,
                            price=getattr(ho, "price_open", 0.0),
                            volume=getattr(ho, "volume_current", getattr(ho, "volume_initial", 0.0)),
                            sl=getattr(ho, "sl", 0.0),
                            tp=getattr(ho, "tp", 0.0),
                        )
        except Exception as exc:
            logger.warning("MT5 history_orders_get failed during dedup: {}", exc)
        return None

    def _to_position_info(self, p: Any) -> PositionInfo:
        direction = "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL"
        # Report the canonical registry symbol, not the broker-native alias
        # (e.g. broker "DE40" → registry "GER40"). Order/modify/close key off the
        # ticket and the raw mt5 position object, so this only affects the symbol
        # the rest of the system records and learns under — keeping pip_size,
        # trade-journal, and per-symbol learner keys consistent.
        return PositionInfo(
            order_id=str(p.ticket),
            symbol=resolve_to_internal(p.symbol),
            direction=direction,
            lots=p.volume,
            open_price=p.price_open,
            current_price=p.price_current,
            sl=p.sl,
            tp=p.tp,
            pnl=p.profit,
            swap=p.swap,
            open_time=datetime.fromtimestamp(p.time, tz=timezone.utc),
            platform="mt5",
        )

    def _fail_order(
        self, symbol: str, direction: str, lots: float, sl: float, tp: float, error: str
    ) -> OrderResult:
        return OrderResult(
            success=False,
            order_id="",
            fill_price=0.0,
            requested_price=0.0,
            slippage_pips=0.0,
            lots=lots,
            symbol=symbol,
            direction=direction.upper(),
            sl=sl,
            tp=tp,
            platform="mt5",
            error=error,
        )
