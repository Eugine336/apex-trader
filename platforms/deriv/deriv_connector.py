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
_REQUEST_TIMEOUT = 20
_MAX_RECONNECT_ATTEMPTS = 10
_PING_INTERVAL = 20       # send keepalive ping every 20 s
_PING_TIMEOUT  = 10       # fail if pong not received within 10 s


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
        self._reconnecting = False
        self._authorized = False
        self._account_id: str = ""
        self._req_id = 0
        self._positions: dict[str, dict] = {}
        self._mapper = SymbolMapper("deriv")

        self._discovered_multipliers: dict[str, list[int]] = {}

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="deriv-ws-loop",
        )
        self._loop_thread.start()
        # asyncio.Lock must be created on self._loop (not the main thread's loop).
        # Creating it in __init__ on the wrong loop causes silent failures in
        # _discover_multipliers, leaving _discovered_multipliers empty and forcing
        # fallback to hardcoded multipliers (often 1000) that Deriv rejects.
        self._lock: asyncio.Lock = asyncio.run_coroutine_threadsafe(
            self._make_lock(), self._loop
        ).result(timeout=5)
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
            self._ws = await websockets.connect(
                url,
                ping_interval=_PING_INTERVAL,
                ping_timeout=_PING_TIMEOUT,
                close_timeout=10,
                open_timeout=30,
            )
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

        await self._discover_multipliers()
        return True

    @staticmethod
    async def _make_lock() -> asyncio.Lock:
        """Create an asyncio.Lock bound to the running loop (self._loop)."""
        return asyncio.Lock()

    async def _discover_multipliers(self) -> None:
        """Query Deriv contracts_for API to discover valid multipliers per symbol."""
        try:
            cfg_path = __file__.replace(
                "platforms/deriv/deriv_connector.py",
                "config/brokers/deriv.json",
            )
            with open(cfg_path) as f:
                cfg = json.load(f)
            symbols = list(cfg.get("multipliers", {}).keys())
        except Exception:
            symbols = []

        for sym in symbols:
            if sym.startswith("_"):
                continue
            try:
                resp = await self._send(
                    {"contracts_for": sym, "currency": "USD", "product_type": "basic"}
                )
                if resp.get("error"):
                    continue
                available = resp.get("contracts_for", {}).get("available", [])
                mults: set[int] = set()
                for contract in available:
                    ctype = contract.get("contract_type", "")
                    if ctype not in ("MULTUP", "MULTDOWN"):
                        continue
                    if "multiplier_range" in contract:
                        mrange = contract["multiplier_range"]
                        for v in mrange:
                            mults.add(int(v))
                    if "multipliers" in contract:
                        for v in contract["multipliers"]:
                            mults.add(int(v))
                if mults:
                    sorted_mults = sorted(mults)
                    self._discovered_multipliers[sym] = sorted_mults
                    logger.info("Deriv multipliers discovered — {}: {}", sym, sorted_mults)
            except Exception as exc:
                logger.debug("Multiplier discovery failed for {}: {}", sym, exc)

    def disconnect(self) -> None:
        if self._ws is not None:
            try:
                future = asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
                future.result(timeout=10)
            except Exception as exc:
                logger.warning("Deriv disconnect error: {}", exc)
        self._connected = False
        self._reconnecting = False
        self._authorized = False
        self._ws = None
        logger.info("Deriv disconnected")

    def is_connected(self) -> bool:
        if self._ws is None or not self._connected:
            return False
        # websockets >= 10 uses .state; older versions expose .open
        state = getattr(self._ws, "state", None)
        if state is not None:
            import websockets.connection as _wsc
            return state == _wsc.State.OPEN
        return bool(getattr(self._ws, "open", False))

    async def _reconnect(self) -> bool:
        self._reconnecting = True
        self._connected = False
        try:
            for attempt in range(1, _MAX_RECONNECT_ATTEMPTS + 1):
                logger.warning("Deriv reconnect attempt {}/{}", attempt, _MAX_RECONNECT_ATTEMPTS)
                ok = await self._connect_async()
                if ok:
                    return True
                await asyncio.sleep(_RECONNECT_DELAY * attempt)
            logger.error("Deriv reconnect failed after {} attempts", _MAX_RECONNECT_ATTEMPTS)
            return False
        finally:
            self._reconnecting = False

    # ── Low-level send/receive ───────────────────────────────────────────

    async def _send(self, payload: dict) -> dict:
        async with self._lock:
            self._req_id += 1
            payload["req_id"] = self._req_id
            for attempt in range(2):  # one retry after reconnect
                try:
                    await self._ws.send(json.dumps(payload))
                    raw = await asyncio.wait_for(self._ws.recv(), timeout=_REQUEST_TIMEOUT)
                    return json.loads(raw)
                except Exception as exc:
                    if attempt == 0:
                        logger.warning("Deriv send error ({}), reconnecting…", exc)
                        ok = await self._reconnect()
                        if not ok:
                            raise ConnectionError("Deriv reconnect failed") from exc
                    else:
                        raise

    def _sync_send(self, payload: dict) -> dict:
        with self._thread_lock:
            if "ticks_history" in payload:
                elapsed = _time.monotonic() - self._last_history_request
                # 0.5s between candle/tick requests — Deriv rate limit is ~3 req/s
                if elapsed < 0.5:
                    _time.sleep(0.5 - elapsed)
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
        # Deriv does not support subscribe=0 on the ticks endpoint.
        # Use ticks_history with count=1 for a one-shot latest price instead.
        resp = self._sync_send({
            "ticks_history": mapped,
            "count": 1,
            "end": "latest",
            "style": "ticks",
            "adjust_start_time": 1,
        })
        if resp.get("error"):
            raise RuntimeError(f"Deriv tick error: {resp['error'].get('message')}")
        history = resp.get("history", {})
        prices = history.get("prices", [])
        times  = history.get("times", [])
        if not prices:
            raise RuntimeError(f"No tick data from Deriv for {mapped}")
        quote = float(prices[-1])
        epoch = int(times[-1]) if times else 0
        # ticks_history returns mid price only — bid/ask not available.
        # Spread is effectively 0 from this endpoint; bootstrap uses it only
        # for instruments where bid/ask are unavailable. For spread purposes
        # the hardcoded registry fallback will be used for synthetics.
        pip_size = get_pip_size(symbol)
        return TickData(
            bid=quote,
            ask=quote,
            spread=0.0,
            time=datetime.fromtimestamp(epoch, tz=timezone.utc),
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

    def _get_multiplier(self, mapped_symbol: str) -> int:
        """Look up the correct multiplier for a Deriv symbol.
        Prefers runtime-discovered values over static JSON config.
        Snaps the default value to the nearest accepted multiplier so Deriv
        never rejects the order with 'Multiplier is not in acceptable range'.
        """
        _FALLBACK_ACCEPTED = [80, 200, 400, 600, 800, 1000, 2000, 4000]

        if mapped_symbol in self._discovered_multipliers:
            accepted = self._discovered_multipliers[mapped_symbol]
        else:
            accepted = None

        try:
            cfg_path = __file__.replace(
                "platforms/deriv/deriv_connector.py",
                "config/brokers/deriv.json"
            )
            with open(cfg_path) as f:
                cfg = json.load(f)
            mult_map = cfg.get("multipliers", {})
            entry = mult_map.get(mapped_symbol) or mult_map.get("_default", {})
            desired = int(entry.get("default", 1000))
            if accepted is None:
                accepted = [int(x) for x in entry.get("accepted", _FALLBACK_ACCEPTED)]
        except Exception:
            desired = 1000
            if accepted is None:
                accepted = _FALLBACK_ACCEPTED

        if not accepted:
            accepted = _FALLBACK_ACCEPTED
        nearest = min(accepted, key=lambda x: abs(x - desired))
        return nearest

    def place_order(
        self,
        symbol: str,
        direction: str,
        lots: float,
        sl: float,
        tp: float,
        comment: str = "",
        # ── Deriv-specific kwargs ──────────────────────────────────────────
        # Pass stake_usd to bypass the lots→stake conversion entirely.
        # PlatformManager sets this when routing a Deriv synthetic order.
        stake_usd: Optional[float] = None,
        multiplier: Optional[int] = None,
    ) -> OrderResult:
        self._require_connection()
        mapped = self.symbol_map(symbol)
        is_buy = direction.upper() in ("BUY", "LONG")

        # Resolve multiplier from broker config if not explicitly passed
        if multiplier is None:
            multiplier = self._get_multiplier(mapped)

        # Use get_price (ticks_history) — avoids subscribe:0 validation error
        _tick = self.get_price(symbol)
        price = _tick.ask if is_buy else _tick.bid

        contract_type = "MULTUP" if is_buy else "MULTDOWN"

        # ── Stake calculation ──────────────────────────────────────────────
        # Deriv Multipliers work on a USD stake, NOT on lots.
        # Formula: stake = risk_amount / (sl_distance_pct * multiplier)
        # where sl_distance_pct = |price - sl| / price
        #
        # If stake_usd is supplied directly (from RiskEngine), use it.
        # Otherwise derive it from the SL distance so the risk stays correct.
        if stake_usd is not None:
            amount = round(max(1.0, stake_usd), 2)
        else:
            sl_distance = abs(price - sl)
            if sl_distance > 0 and price > 0:
                # Risk% of account that the SL represents at this multiplier:
                # P&L = stake × multiplier × (Δprice / price)
                # Max loss = stake × multiplier × (sl_distance / price)
                # → stake = max_loss / (multiplier × sl_distance / price)
                # lots here carries the risk_amount already encoded by the sizer,
                # so we back-calculate: risk_amount = lots * risk_pips * pip_value
                # For synthetics pip_value = 1.0 (from registry)
                pip = get_pip_size(symbol)
                risk_pips = sl_distance / pip if pip else sl_distance
                risk_amount = lots * risk_pips * 1.0   # pip_value_per_lot = 1 for synthetics
                stake = risk_amount / (multiplier * sl_distance / price)
                amount = round(max(1.0, stake), 2)
            else:
                # Fallback: 1% of account proxy — will be overridden by stake_usd path
                amount = round(max(1.0, lots * 10), 2)

        logger.debug(
            "Deriv stake — {} {} | price={} SL={} | stake=${} multiplier={}×",
            direction, symbol, price, sl, amount, multiplier,
        )

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
                "multiplier": multiplier,
                "limit_order": {
                    "stop_loss": round(abs(price - sl) / price * amount * multiplier, 2),
                    "take_profit": round(abs(tp - price) / price * amount * multiplier, 2),
                },
            },
        })
        latency = (_time.monotonic() - t0) * 1000

        # ── Auto-retry loop ────────────────────────────────────────────────
        # Deriv can reject an order for two independent reasons that interact:
        #   1. Wrong multiplier  → fix multiplier, rescale stake to preserve risk
        #   2. Stake cap         → floor stake to Deriv's cap for this symbol/price
        #
        # The old nested-if approach fired cap-check BEFORE multiplier-check, so
        # when both were wrong it chased a moving cap at the wrong multiplier.
        # This loop handles them in the correct order (multiplier first) and re-runs
        # up to MAX_RETRIES times so a cap that shifts between requests is caught.
        import re as _re
        import math as _math

        MAX_RETRIES = 4
        err: Optional[str] = resp["error"].get("message", "Unknown error") if resp.get("error") else None

        for _attempt in range(MAX_RETRIES):
            if err is None:
                break  # success

            # ── 1. Multiplier correction (always fix this first) ───────────
            _mult_match = _re.search(
                r"Multiplier is not in acceptable range.*?Accepts\s+([\d,\s]+)", err
            )
            if _mult_match:
                valid = sorted(int(x.strip()) for x in _mult_match.group(1).split(",") if x.strip().isdigit())
                if valid:
                    corrected = min(valid, key=lambda x: abs(x - multiplier))
                    logger.warning(
                        "Deriv multiplier {} rejected for {} — retrying with {} (valid: {}). "
                        "Update config/brokers/deriv.json!",
                        multiplier, mapped, corrected, valid,
                    )
                    # Rescale stake so max-loss in dollars stays constant:
                    # max_loss = stake × mult × sl_pct  →  new_stake = stake × old_mult / new_mult
                    if corrected > 0:
                        amount = round(max(1.0, amount * multiplier / corrected), 2)
                        logger.debug(
                            "Deriv stake rescaled for {}× → {}×: ${:.2f}",
                            multiplier, corrected, amount,
                        )
                    multiplier = corrected

            # ── 2. Stake cap ───────────────────────────────────────────────
            _cap_match = _re.search(r"equal to or lower than ([\d]+(?:\.[\d]+)?)", err)
            if _cap_match:
                max_stake = float(_cap_match.group(1))
                # Floor to cent — keeps us cleanly under the cap without
                # giving up more than $0.01 of stake.
                capped = max(1.0, float(_math.floor(max_stake * 100)) / 100)
                if capped < amount:
                    logger.warning(
                        "Deriv stake capped — retrying {} {} with ${:.2f} (cap ${:.2f}), attempt {}/{}",
                        direction, symbol, capped, max_stake, _attempt + 1, MAX_RETRIES,
                    )
                    amount = capped

            # ── 3. Retry with updated multiplier + amount ──────────────────
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
                    "multiplier": multiplier,
                    "limit_order": {
                        "stop_loss": round(abs(price - sl) / price * amount * multiplier, 2),
                        "take_profit": round(abs(tp - price) / price * amount * multiplier, 2),
                    },
                },
            })
            err = resp["error"].get("message", "Unknown error") if resp.get("error") else None

        if err:
            logger.error("Deriv order failed — {} {} {}: {}", direction, mapped, lots, err)
            return OrderResult(
                success=False, order_id="", fill_price=0.0,
                requested_price=price, slippage_pips=0.0, lots=lots,
                symbol=symbol, direction=direction.upper(), sl=sl, tp=tp,
                platform="deriv", error=err,
            )

        buy_resp = resp.get("buy", {})
        contract_id = str(buy_resp.get("contract_id", ""))

        self._positions[contract_id] = {
            "symbol": symbol, "direction": direction.upper(),
            "lots": lots, "sl": sl, "tp": tp, "open_price": price,
        }

        logger.info(
            "Deriv order filled — {} {} {} lots @ {} ({:.0f}ms)",
            direction, mapped, lots, amount, latency,
        )
        return OrderResult(
            success=True,
            order_id=contract_id,
            fill_price=price,
            requested_price=price,
            slippage_pips=0.0,
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
        if self._reconnecting:
            raise ConnectionError("Deriv is reconnecting — request blocked")
        if not self._connected or self._ws is None:
            raise ConnectionError("Deriv is not connected")
