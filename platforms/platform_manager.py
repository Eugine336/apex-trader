"""
APEX TRADER — Unified Platform Manager
One interface to rule them all.
Routes trades to the right platform based on instrument type.
Two arms, one mind. MT5 for Forex/indices. Deriv for synthetics — 24/7.
"""

import os
import time as _time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from loguru import logger

from config import (
    INSTRUMENT_REGISTRY,
    AppConfig,
    InstrumentCategory,
    Platform,
    get_all_symbols,
    get_instrument,
    get_instruments_by_platform,
    get_pip_size,
)
from platforms.base_connector import (
    AccountInfo,
    BaseConnector,
    CloseResult,
    OrderResult,
    PositionInfo,
    TickData,
)
from platforms.deriv.deriv_connector import DerivConnector
from platforms.mt5.mt5_connector import MT5Connector


class PlatformManager:
    """Routes every operation to the correct platform connector."""

    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or AppConfig()

        self.mt5 = MT5Connector(
            login=int(os.getenv("MT5_LOGIN", "0")),
            password=os.getenv("MT5_PASSWORD", ""),
            server=os.getenv("MT5_SERVER", ""),
        )
        self.deriv = DerivConnector(
            api_token=os.getenv("DERIV_API_TOKEN", ""),
            app_id=os.getenv("DERIV_APP_ID", ""),
        )

        self._mt5_connected = False
        self._deriv_connected = False

    # ── Connection management ────────────────────────────────────────────

    def connect_all(self) -> dict[str, bool]:
        results: dict[str, bool] = {}

        logger.info("Connecting to MT5…")
        self._mt5_connected = self.mt5.connect()
        results["mt5"] = self._mt5_connected
        if self._mt5_connected:
            logger.info("MT5 — ONLINE")
        else:
            logger.warning("MT5 — OFFLINE (non-Windows or credentials missing)")

        logger.info("Connecting to Deriv…")
        self._deriv_connected = self.deriv.connect()
        results["deriv"] = self._deriv_connected
        if self._deriv_connected:
            logger.info("Deriv — ONLINE")
        else:
            logger.warning("Deriv — OFFLINE (credentials missing or connection failed)")

        if not any(results.values()):
            logger.error("No platforms connected — trading disabled")
        return results

    def disconnect_all(self) -> None:
        self.mt5.disconnect()
        self.deriv.disconnect()
        self._mt5_connected = False
        self._deriv_connected = False
        logger.info("All platforms disconnected")

    @property
    def any_connected(self) -> bool:
        return self._mt5_connected or self._deriv_connected

    # ── Routing ──────────────────────────────────────────────────────────

    def get_connector(self, symbol: str) -> BaseConnector:
        """Route a symbol to the correct platform connector."""
        try:
            info = get_instrument(symbol)
        except KeyError:
            if self._mt5_connected:
                return self.mt5
            if self._deriv_connected:
                return self.deriv
            raise ConnectionError(f"No platform available for {symbol}")

        if info.platform == Platform.DERIV:
            if self._deriv_connected:
                return self.deriv
            raise ConnectionError(f"Deriv not connected for {symbol}")

        if info.platform == Platform.MT5:
            if self._mt5_connected:
                return self.mt5
            raise ConnectionError(f"MT5 not connected for {symbol}")

        if self._mt5_connected:
            return self.mt5
        if self._deriv_connected:
            return self.deriv
        raise ConnectionError(f"No platform connected for {symbol}")

    def get_platform_name(self, symbol: str) -> str:
        connector = self.get_connector(symbol)
        return "mt5" if isinstance(connector, MT5Connector) else "deriv"

    # ── Trade execution ──────────────────────────────────────────────────

    def execute_entry(
        self,
        symbol: str,
        direction: str,
        lots: float,
        sl: float,
        tp: float,
        comment: str = "",
        stake_usd: Optional[float] = None,  # Deriv only — risk amount in USD
    ) -> OrderResult:
        connector = self.get_connector(symbol)
        platform = "mt5" if isinstance(connector, MT5Connector) else "deriv"

        t0 = _time.monotonic()
        if isinstance(connector, DerivConnector):
            result = connector.place_order(
                symbol, direction, lots, sl, tp, comment,
                stake_usd=stake_usd,
            )
        else:
            result = connector.place_order(symbol, direction, lots, sl, tp, comment)
        latency_ms = (_time.monotonic() - t0) * 1000

        if result.success:
            logger.info(
                "[{}] ENTRY {} {} {:.2f} lots @ {:.5f} | slip {:.1f}pip | {:.0f}ms",
                platform.upper(), direction, symbol, lots,
                result.fill_price, result.slippage_pips, latency_ms,
            )
        else:
            logger.error(
                "[{}] ENTRY FAILED {} {} {:.2f} lots: {}",
                platform.upper(), direction, symbol, lots, result.error,
            )
        return result

    def modify_trade(
        self,
        order_id: str,
        platform: str,
        new_sl: Optional[float] = None,
        new_tp: Optional[float] = None,
    ) -> bool:
        connector = self.mt5 if platform == "mt5" else self.deriv
        return connector.modify_order(order_id, new_sl, new_tp)

    def close_trade(
        self,
        order_id: str,
        platform: str,
        lots: Optional[float] = None,
    ) -> CloseResult:
        connector = self.mt5 if platform == "mt5" else self.deriv
        return connector.close_order(order_id, lots)

    # ── Positions ────────────────────────────────────────────────────────

    def get_all_open_positions(self) -> list[PositionInfo]:
        positions: list[PositionInfo] = []
        if self._mt5_connected:
            try:
                positions.extend(self.mt5.get_open_positions())
            except Exception as exc:
                logger.warning("MT5 positions fetch error: {}", exc)
        if self._deriv_connected:
            try:
                positions.extend(self.deriv.get_open_positions())
            except Exception as exc:
                logger.warning("Deriv positions fetch error: {}", exc)
        return positions

    # ── Account ──────────────────────────────────────────────────────────

    def get_account_summary(self) -> dict[str, AccountInfo]:
        summary: dict[str, AccountInfo] = {}
        if self._mt5_connected:
            try:
                summary["mt5"] = self.mt5.get_account_info()
            except Exception as exc:
                logger.warning("MT5 account info error: {}", exc)
        if self._deriv_connected:
            try:
                summary["deriv"] = self.deriv.get_account_info()
            except Exception as exc:
                logger.warning("Deriv account info error: {}", exc)
        return summary

    def get_total_balance(self) -> float:
        total = 0.0
        for info in self.get_account_summary().values():
            total += info.balance
        return total

    def get_platform_balance(self, symbol: str) -> float:
        """Get balance for the specific platform that handles this symbol."""
        try:
            connector = self.get_connector(symbol)
        except Exception as exc:
            logger.warning("Balance lookup failed for {}: {}", symbol, exc)
            return 0.0

        platform_name = "mt5" if isinstance(connector, MT5Connector) else "deriv"

        if isinstance(connector, MT5Connector) and self._mt5_connected:
            try:
                balance = float(self.mt5.get_account_info().balance)
                logger.debug(
                    "Balance for {} → {} platform: ${:.2f}",
                    symbol,
                    platform_name,
                    balance,
                )
                return balance
            except Exception as exc:
                logger.warning("MT5 balance fetch error for {}: {}", symbol, exc)
                return 0.0

        if isinstance(connector, DerivConnector) and self._deriv_connected:
            try:
                balance = float(self.deriv.get_account_info().balance)
                logger.debug(
                    "Balance for {} → {} platform: ${:.2f}",
                    symbol,
                    platform_name,
                    balance,
                )
                return balance
            except Exception as exc:
                logger.warning("Deriv balance fetch error for {}: {}", symbol, exc)
                return 0.0

        return 0.0

    def get_total_equity(self) -> float:
        total = 0.0
        for info in self.get_account_summary().values():
            total += info.equity
        return total

    # ── Market data ──────────────────────────────────────────────────────

    def fetch_market_data(
        self,
        symbol: str,
        timeframes: Optional[list[str]] = None,
        count: int = 200,
    ) -> dict[str, pd.DataFrame]:
        """Fetch OHLCV across multiple timeframes for a single symbol."""
        if timeframes is None:
            timeframes = ["H4", "H1", "M15", "M5"]
        connector = self.get_connector(symbol)
        data: dict[str, pd.DataFrame] = {}

        # Track symbols confirmed unavailable on the broker so we only log once.
        if not hasattr(self, "_unavailable_symbols"):
            self._unavailable_symbols: set = set()

        for tf in timeframes:
            try:
                data[tf] = connector.get_ohlcv(symbol, tf, count)
            except Exception as exc:
                exc_str = str(exc)
                # "not available on broker" → permanent skip; log once only.
                if "not available on broker" in exc_str or "not found" in exc_str.lower():
                    if symbol not in self._unavailable_symbols:
                        logger.warning(
                            "Symbol '{}' not available on broker — skipping permanently. "
                            "Remove it from enabled_symbols_override or add a broker mapping.",
                            symbol,
                        )
                        self._unavailable_symbols.add(symbol)
                    break  # no point trying other timeframes for this symbol
                else:
                    logger.warning("Data fetch failed — {} {}: {}", symbol, tf, exc)
        return data

    def fetch_all_market_data(
        self,
        symbols: Optional[list[str]] = None,
        timeframes: Optional[list[str]] = None,
        count: int = 200,
    ) -> dict[str, dict[str, pd.DataFrame]]:
        """Fetch OHLCV for every enabled symbol across timeframes."""
        from datetime import timezone
        import datetime as _dt
        from config import is_always_open

        if symbols is None:
            symbols = self.config.enabled_pairs
        if timeframes is None:
            timeframes = ["H4", "H1", "M15", "M5"]

        # On weekends skip non-24/7 instruments (forex, indices, commodities are closed)
        now_utc = _dt.datetime.now(timezone.utc)
        is_weekend = now_utc.weekday() >= 5  # 5=Saturday, 6=Sunday

        all_data: dict[str, dict[str, pd.DataFrame]] = {}
        for symbol in symbols:
            # Skip closed instruments on weekends
            if is_weekend and not is_always_open(symbol):
                continue
            # Skip symbols already confirmed as broker-unavailable (log only once)
            if hasattr(self, "_unavailable_symbols") and symbol in self._unavailable_symbols:
                continue
            try:
                data = self.fetch_market_data(symbol, timeframes, count)
                if data:
                    all_data[symbol] = data
            except ConnectionError:
                logger.debug("Skipping {} — no platform available", symbol)
            except Exception as exc:
                logger.warning("Market data error for {}: {}", symbol, exc)
        return all_data

    def get_price(self, symbol: str) -> TickData:
        return self.get_connector(symbol).get_price(symbol)

    def get_spread(self, symbol: str) -> float:
        return self.get_connector(symbol).get_spread(symbol)
