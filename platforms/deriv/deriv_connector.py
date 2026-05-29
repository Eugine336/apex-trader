"""
APEX TRADER — Deriv WebSocket Connector
Connects to Deriv via the official WebSocket API.
Handles synthetics (V75, Boom/Crash) 24/7 and Forex on Deriv.
"""

import asyncio
import json
import threading
import time as _time
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd
from loguru import logger

from brain.symbol_mapper import SymbolMapper
from config import get_pip_size
from platforms.base_connector import (
    AccountInfo,
    BaseConnector,
    CloseResult,
    OrderResult,
    PositionInfo,
    TickData,
)

try:
    import websockets  # type: ignore[import-untyped]
    import websockets.client  # type: ignore[import-untyped]

    _WS_AVAILABLE = True
except ImportError:
    websockets = None  # type: ignore[assignment]
    _WS_AVAILABLE = False

_DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id={app_id}"

_GRANULARITY_MAP: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}

_RECONNECT_DELAY = 5
_REQUEST_TIMEOUT = 15
_MAX_RECONNECT_ATTEMPTS = 5


class DerivConnector(BaseConnector):
    """Deriv WebSocket platform connector."""

    def __init__(
        self,
        api_token: str = "",
        app_id: str = "",
    ):
        self._api_token = api_token
        self._app_id = app_id
        self._ws: Any = None
        self._connected = False
        self._authorized = False
        self._account_id: str = ""
        self._req_id = 0
        self._positions: dict[str, dict] = {}
        self._mapper = SymbolMapper("deriv")

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="deriv-ws-loop",
        )
        self._loop_thread.start()
        self._lock = asyncio.Lock()
        self._thread_lock = threading.Lock()
        self._last_history_request: float = 0.0

    # ── Connection ───────────────────────────────────────────────────────

    def connect(self) -> bool:
        if not _WS_AVAILABLE:
            logger.error("websockets package not installed")
            return False
        if not self._app_id:
            logger.error("Deriv app_id not provided")
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(self._connect_async(), self._loop)
            return future.result(timeout=30)
        except Exception as exc:
            logger.error("Deriv connect error: {}", exc)
            return False

    async def _connect_async(self) -> bool:
        url = _DERIV_WS_URL.format(app_id=self._app_id)
        try:
            self._ws = await websockets.connect(url, ping_interval=30, close_timeout=10)
        except Exception as exc:
            logger.error("Deriv WS connect failed: {}", exc)
            return False

        if self._api_token:
            resp = await self._send({"authorize": self._api_token})
            if resp.get("error"):
                logger.error("Deriv auth failed: {}", resp["error"].get("message"))
                await self._ws.close()
                return False
            auth = resp.get("authorize", {})
            self._account_id = auth.get("loginid", "")
            self._authorized = True
            logger.info(
                "Deriv connected — account {} | balance {} {}",
                self._account_id,
                auth.get("balance", 0),
                auth.get("currency", "USD"),
            )
        self._connected = True
        return True

    def disconnect(self) -> None:
        if self._ws is not None:
            try:
                future = asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
                future.result(timeout=10)
            except Exception as exc:
                logger.warning("Deriv disconnect error: {}", exc)
        self._connected = False
        self._authorized = False
        self._ws = None
        logger.info("Deriv disconnected")

    def is_connected(self) -> bool:
        if self._ws is None or not self._connected:
            return False
        return self._ws.open

    async def _reconnect(self) -> bool:
        for attempt in range(1, _MAX_RECONNECT_ATTEMPTS + 1):
            logger.warning("Deriv reconnect attempt {}/{}", attempt, _MAX_RECONNECT_ATTEMPTS)
            ok = await self._connect_async()
            if ok:
                return True
            await asyncio.sleep(_RECONNECT_DELAY * attempt)
        logger.error("Deriv reconnect failed after {} attempts", _MAX_RECONNECT_ATTEMPTS)
        return False

    # ── Low-level send/receive ───────────────────────────────────────────

    async def _send(self, payload: dict) -> dict:
        async with self._lock:
            self._req_id += 1
            payload["req_id"] = self._req_id
            await self._ws.send(json.dumps(payload))
            raw = await asyncio.wait_for(self._ws.recv(), timeout=_REQUEST_TIMEOUT)
            return json.loads(raw)

    def _sync_send(self, payload: dict) -> dict:
        with self._thread_lock:
            if "ticks_history" in payload:
                elapsed = _time.monotonic() - self._last_history_request
                if elapsed < 0.25:
                    _time.sleep(0.25 - elapsed)
                self._last_history_request = _time.monotonic()
            future = asyncio.run_coroutine_threadsafe(self._send(payload), self._loop)
            return future.result(timeout=_REQUEST_TIMEOUT + 5)

    # ── Account ──────────────────────────────────────────────────────────

    def get_account_info(self) -> AccountInfo:
        self._require_connection()
        resp = self._sync_send({"balance": 1, "subscribe": 0})
        bal = resp.get("balance", {})
        return AccountInfo(
            balance=float(bal.get("balance", 0)),
            equity=float(bal.get("balance", 0)),
            margin=0.0,
            free_margin=float(bal.get("balance", 0)),
            margin_level=0.0,
            currency=bal.get("currency", "USD"),
            leverage=1,
            platform="deriv",
        )

    # ── Market data ──────────────────────────────────────────────────────

    def get_price(self, symbol: str) -> TickData:
        self._require_connection()
        mapped = self.symbol_map(symbol)
        resp = self._sync_send({"ticks": mapped, "subscribe": 0})
        if resp.get("error"):
            raise RuntimeError(f"Deriv tick error: {resp['error'].get('message')}")
        tick = resp.get("tick", {})
        bid = float(tick.get("bid", tick.get("quote", 0)))
        ask = float(tick.get("ask", bid))
        pip_size = get_pip_size(symbol)
        spread = round((ask - bid) / pip_size, 1) if ask > bid else 0.0
        return TickData(
            bid=bid,
            ask=ask,
            spread=spread,
            time=datetime.fromtimestamp(tick.get("epoch", 0), tz=timezone.utc),
        )

    def get_tick(self, symbol: str) -> TickData:
        return self.get_price(symbol)

    def get_ohlcv(
        self, symbol: str, timeframe: str, count: int = 200
    ) -> pd.DataFrame:
        self._require_connection()
        mapped = self.symbol_map(symbol)
        granularity = self.timeframe_map(timeframe)
        resp = self._sync_send({
            "ticks_history": mapped,
            "adjust_start_time": 1,
            "count": count,
            "end": "latest",
            "granularity": granularity,
            "style": "candles",
        })
        if resp.get("error"):
            raise RuntimeError(
                f"Deriv candles error for {mapped}: {resp['error'].get('message')}"
            )
        candles = resp.get("candles", [])
        if not candles:
            raise RuntimeError(f"No candle data for {mapped}/{timeframe}")

        rows = []
        for c in candles:
            rows.append({
                "time": datetime.fromtimestamp(c["epoch"], tz=timezone.utc),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": int(c.get("volume", 0)),
            })
        return pd.DataFrame(rows)

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
        is_buy = direction.upper() in ("BUY", "LONG")

        tick_resp = self._sync_send({"ticks": mapped, "subscribe": 0})
        tick = tick_resp.get("tick", {})
        price = float(tick.get("ask" if is_buy else "bid", tick.get("quote", 0)))

        contract_type = "MULTUP" if is_buy else "MULTDOWN"
        amount = round(lots * 100, 2)

        t0 = _time.monotonic()
        resp = self._sync_send({
            "buy": 1,
            "subscribe": 1,
            "price": amount,
            "parameters": {
                "contract_type": contract_type,
                "symbol": mapped,
                "currency": "USD",
                "amount": amount,
                "basis": "stake",
                "multiplier": 100,
                "limit_order": {
                    "stop_loss": round(abs(price - sl) * lots * 100, 2),
                    "take_profit": round(abs(tp - price) * lots * 100, 2),
                },
            },
        })
        latency = (_time.monotonic() - t0) * 1000

        if resp.get("error"):
            err = resp["error"].get("message", "Unknown error")
            logger.error("Deriv order failed — {} {} {}: {}", direction, mapped, lots, err)
            return OrderResult(
                success=False, order_id="", fill_price=0.0,
                requested_price=price, slippage_pips=0.0, lots=lots,
                symbol=symbol, direction=direction.upper(), sl=sl, tp=tp,
                platform="deriv", error=err,
            )

        buy_resp = resp.get("buy", {})
        contract_id = str(buy_resp.get("contract_id", ""))
        fill = float(buy_resp.get("buy_price", price))
        pip_size = get_pip_size(symbol)
        slippage = abs(fill - price) / pip_size if pip_size else 0.0

        self._positions[contract_id] = {
            "symbol": symbol, "direction": direction.upper(),
            "lots": lots, "sl": sl, "tp": tp, "open_price": price,
        }

        logger.info(
            "Deriv order filled — {} {} {} lots @ {} ({:.0f}ms)",
            direction, mapped, lots, fill, latency,
        )
        return OrderResult(
            success=True,
            order_id=contract_id,
            fill_price=fill,
            requested_price=price,
            slippage_pips=round(slippage, 2),
            lots=lots,
            symbol=symbol,
            direction=direction.upper(),
            sl=sl,
            tp=tp,
            platform="deriv",
        )

    def modify_order(
        self,
        order_id: str,
        new_sl: Optional[float] = None,
        new_tp: Optional[float] = None,
    ) -> bool:
        self._require_connection()
        limit_order: dict[str, Any] = {}
        pos = self._positions.get(order_id, {})
        open_price = pos.get("open_price", 0)
        lots = pos.get("lots", 1)

        if new_sl is not None:
            limit_order["stop_loss"] = round(abs(open_price - new_sl) * lots * 100, 2)
        if new_tp is not None:
            limit_order["take_profit"] = round(abs(new_tp - open_price) * lots * 100, 2)

        if not limit_order:
            return True

        resp = self._sync_send({
            "contract_update": 1,
            "contract_id": int(order_id),
            "limit_order": limit_order,
        })
        if resp.get("error"):
            logger.error("Deriv modify failed for {}: {}", order_id, resp["error"].get("message"))
            return False

        if new_sl is not None and order_id in self._positions:
            self._positions[order_id]["sl"] = new_sl
        if new_tp is not None and order_id in self._positions:
            self._positions[order_id]["tp"] = new_tp
        logger.info("Deriv modified {} — SL={} TP={}", order_id, new_sl, new_tp)
        return True

    def close_order(
        self, order_id: str, lots: Optional[float] = None
    ) -> CloseResult:
        self._require_connection()
        resp = self._sync_send({"sell": int(order_id), "price": 0})
        if resp.get("error"):
            err = resp["error"].get("message", "Unknown")
            logger.error("Deriv close failed for {}: {}", order_id, err)
            return CloseResult(
                success=False, order_id=order_id, close_price=0.0,
                lots_closed=0.0, pnl=0.0, platform="deriv", error=err,
            )

        sell_resp = resp.get("sell", {})
        pnl = float(sell_resp.get("sold_for", 0)) - float(sell_resp.get("buy_price", 0))
        pos = self._positions.pop(order_id, {})

        logger.info("Deriv closed {} — PnL {:.2f}", order_id, pnl)
        return CloseResult(
            success=True,
            order_id=order_id,
            close_price=float(sell_resp.get("sold_for", 0)),
            lots_closed=pos.get("lots", lots or 0),
            pnl=pnl,
            platform="deriv",
        )

    # ── Positions ────────────────────────────────────────────────────────

    def get_open_positions(self) -> list[PositionInfo]:
        self._require_connection()
        resp = self._sync_send({
            "portfolio": 1,
            "contract_type": ["MULTUP", "MULTDOWN"],
        })
        contracts = resp.get("portfolio", {}).get("contracts", [])
        positions: list[PositionInfo] = []
        for c in contracts:
            cid = str(c.get("contract_id", ""))
            local = self._positions.get(cid, {})
            direction = "BUY" if c.get("contract_type") == "MULTUP" else "SELL"
            positions.append(PositionInfo(
                order_id=cid,
                symbol=local.get("symbol", c.get("symbol", "")),
                direction=direction,
                lots=local.get("lots", 0),
                open_price=float(c.get("buy_price", 0)),
                current_price=float(c.get("bid_price", 0)),
                sl=local.get("sl", 0),
                tp=local.get("tp", 0),
                pnl=float(c.get("profit", 0)),
                swap=0.0,
                open_time=datetime.fromtimestamp(
                    c.get("date_start", 0), tz=timezone.utc
                ),
                platform="deriv",
            ))
        return positions

    def get_position_info(self, order_id: str) -> Optional[PositionInfo]:
        self._require_connection()
        resp = self._sync_send({
            "proposal_open_contract": 1,
            "contract_id": int(order_id),
        })
        if resp.get("error"):
            return None
        poc = resp.get("proposal_open_contract", {})
        local = self._positions.get(order_id, {})
        direction = "BUY" if poc.get("contract_type") == "MULTUP" else "SELL"
        return PositionInfo(
            order_id=order_id,
            symbol=local.get("symbol", poc.get("underlying", "")),
            direction=direction,
            lots=local.get("lots", 0),
            open_price=float(poc.get("buy_price", 0)),
            current_price=float(poc.get("bid_price", 0)),
            sl=local.get("sl", 0),
            tp=local.get("tp", 0),
            pnl=float(poc.get("profit", 0)),
            swap=0.0,
            open_time=datetime.fromtimestamp(
                poc.get("date_start", 0), tz=timezone.utc
            ),
            platform="deriv",
        )

    # ── Mapping ──────────────────────────────────────────────────────────

    def symbol_map(self, apex_symbol: str) -> str:
        return self._mapper.to_broker(apex_symbol)

    def timeframe_map(self, tf: str) -> int:
        return _GRANULARITY_MAP.get(tf.upper(), 3600)

    # ── Private helpers ──────────────────────────────────────────────────

    def _require_connection(self) -> None:
        if not self._connected or self._ws is None:
            raise ConnectionError("Deriv is not connected")
