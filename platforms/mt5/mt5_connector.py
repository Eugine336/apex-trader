"""
APEX TRADER — MetaTrader 5 Connector
Connects to MetaTrader 5 via the official Python package.
Handles Forex, commodities, and indices on MT5.
"""

import platform as sys_platform
import time as _time
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd
from loguru import logger

from config import get_instrument, get_pip_size
from brain.symbol_mapper import SymbolMapper
from platforms.base_connector import (
    AccountInfo,
    BaseConnector,
    CloseResult,
    OrderResult,
    OrderStatus,
    PositionInfo,
    TickData,
)

_MT5_AVAILABLE = False
try:
    import MetaTrader5 as mt5  # type: ignore[import-untyped]

    _MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore[assignment]


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
    ):
        self._login = login
        self._password = password
        self._server = server
        self._deviation = deviation
        self._magic = magic
        self._connected = False
        self._symbol_cache: dict[str, str] = {}
        self._broker_name = broker_name
        self._mapper = SymbolMapper(broker_name)  # updated after connect() if auto

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
        except Exception:
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

    # ── Order execution ──────────────────────────────────────────────────

    def place_order(
        self,
        symbol: str,
        direction: str,
        lots: float,
        sl: float,
        tp: float,
        comment: str = "",
    ) -> OrderResult:
        self._require_connection()
        mapped = self.symbol_map(symbol)
        tick = mt5.symbol_info_tick(mapped)
        if tick is None:
            return self._fail_order(symbol, direction, lots, sl, tp, "No tick data")

        is_buy = direction.upper() in ("BUY", "LONG")
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        price = tick.ask if is_buy else tick.bid

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
            "comment": comment or "APEX",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        t0 = _time.monotonic()
        result = mt5.order_send(request)
        latency = (_time.monotonic() - t0) * 1000

        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(mt5.last_error())
            logger.error("MT5 order failed — {} {} {} lots: {}", direction, mapped, lots, err)
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

    # ── Symbol / timeframe mapping ───────────────────────────────────────

    def symbol_map(self, apex_symbol: str) -> str:
        if apex_symbol in self._symbol_cache:
            return self._symbol_cache[apex_symbol]

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
        self._symbol_cache[apex_symbol] = apex_symbol
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