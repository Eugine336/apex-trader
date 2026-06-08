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
from brain.symbol_mapper import SymbolMapper
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
    ):
        self._login = login
        self._password = password
        self._server = server
        self._deviation = deviation
        self._magic = magic
        self._connected = False
        self._symbol_cache: dict[str, Optional[str]] = {}  # None = confirmed not on broker
        self._not_found_warned: set[str] = set()  # warn once then silent
        self._broker_name = broker_name
        self._reject_on_minlot_inflation = reject_on_minlot_inflation
        # Defer SymbolMapper creation when broker_name is "auto".
        # connect() will detect the real broker name and create the mapper then.
        # Creating it now with "auto" would trigger a "no config" warning.
        self._mapper: Optional[SymbolMapper] = (
            None if broker_name == "auto" else SymbolMapper(broker_name)
        )

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
                info.login,
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

    # ── Market data ──────────────────────────────────────────────────────

    def get_price(self, symbol: str) -> TickData:
        self._require_connection()
        mapped = self.symbol_map(symbol)
        tick = mt5.symbol_info_tick(mapped)
        if tick is None:
            raise RuntimeError(f"No tick data for {mapped}: {mt5.last_error()}")
        pip_size = get_pip_size(symbol)
        return TickData(
            bid=tick.bid,
            ask=tick.ask,
            spread=round((tick.ask - tick.bid) / pip_size, 1),
            time=datetime.fromtimestamp(tick.time, tz=timezone.utc),
        )

    def get_tick(self, symbol: str) -> TickData:
        return self.get_price(symbol)

    def get_ohlcv(
        self, symbol: str, timeframe: str, count: int = 200
    ) -> pd.DataFrame:
        self._require_connection()
        mapped = self.symbol_map(symbol)
        # None means already confirmed not on this broker — skip immediately
        if self._symbol_cache.get(symbol) is None and symbol in self._symbol_cache:
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
        return df[["time", "open", "high", "low", "close", "volume"]]

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

        is_buy = direction.upper() in ("BUY", "LONG")
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        price = tick.ask if is_buy else tick.bid

        order_comment = comment or "APEX"

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

        # Stops: enforce minimum SL/TP distance from entry price
        if stops_level > 0:
            min_distance = stops_level * point
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

        t0 = _time.monotonic()
        result = mt5.order_send(request)
        latency = (_time.monotonic() - t0) * 1000

        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(mt5.last_error())
            logger.error("MT5 order failed — {} {} {} lots: {}", direction, mapped, lots, err)
            # Tag market-closed errors so the execution circuit breaker upstream
            # does NOT count them as real failures and open a cooldown window.
            # A closed market is expected and temporary — not an execution problem.
            if "market closed" in err.lower() or "market is closed" in err.lower():
                return self._fail_order(symbol, direction, lots, sl, tp, f"MARKET_CLOSED: {err}")
            return self._fail_order(symbol, direction, lots, sl, tp, err)

        pip_size = get_pip_size(symbol)
        slippage = abs(result.price - price) / pip_size

        logger.info(
            "MT5 order filled — {} {} {} lots @ {} (slip {:.1f} pip, {:.0f}ms)",
            direction, mapped, lots, result.price, slippage, latency,
        )
        return OrderResult(
            success=True,
            order_id=str(result.order),
            fill_price=result.price,
            requested_price=price,
            slippage_pips=round(slippage, 2),
            lots=lots,
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

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": int(order_id),
            "symbol": position.symbol,
            "sl": float(new_sl) if new_sl is not None else position.sl,
            "tp": float(new_tp) if new_tp is not None else position.tp,
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
        Returns None if the deal history isn't available."""
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
            total_pnl = sum(d.profit + d.commission + d.swap + d.fee for d in deals)
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
        return apex_symbol

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
        return PositionInfo(
            order_id=str(p.ticket),
            symbol=p.symbol,
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
