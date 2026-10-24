from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .schema import ChatGPTTradeSignal


@dataclass(frozen=True)
class ExecutionResult:
    signal_id: str
    symbol: str
    side: str
    status: str
    order_id: str | None
    fill_price: float | None
    executed_qty: float
    stop_order_id: str | None = None
    take_profit_order_id: str | None = None
    mode: str = "PAPER"


class BinanceExecutionError(RuntimeError):
    pass


class BinanceSignedClient:
    """Minimal USD-M Futures signed REST client.

    Only account, position, exchange-info, ticker and order endpoints are used.
    No withdrawal/transfer endpoint exists in this client.
    """

    BASE = os.getenv("BINANCE_FUTURES_BASE_URL", "https://fapi.binance.com").rstrip("/")

    def __init__(self) -> None:
        self.key = os.getenv("BINANCE_API_KEY", "")
        self.secret = os.getenv("BINANCE_API_SECRET", "")
        if not self.key or not self.secret:
            raise BinanceExecutionError("BINANCE_API_KEY and BINANCE_API_SECRET are required")

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None, signed: bool = False) -> Any:
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params.setdefault("recvWindow", 5000)
            query = urllib.parse.urlencode(params, doseq=True)
            signature = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            query += "&signature=" + signature
        else:
            query = urllib.parse.urlencode(params, doseq=True)
        url = self.BASE + path + (("?" + query) if query else "")
        headers = {"X-MBX-APIKEY": self.key, "User-Agent": "APEX-ChatGPT-Executor/1.0"}
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except Exception as exc:
            raise BinanceExecutionError(f"Binance request failed: {method} {path}: {exc}") from exc

    def account(self) -> Any:
        return self._request("GET", "/fapi/v2/account", signed=True)

    def exchange_info(self, symbol: str) -> Any:
        return self._request("GET", "/fapi/v1/exchangeInfo", {"symbol": symbol})

    def book_ticker(self, symbol: str) -> Any:
        return self._request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})

    def position_risk(self, symbol: str) -> Any:
        return self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)

    def place_order(self, **params: Any) -> Any:
        """Block direct order submission outside the canonical Brain path."""
        raise BinanceExecutionError(
            "Direct Binance order placement is disabled: external signals are not "
            "an Apex trade authority. A first-class CognitiveBrain opportunity "
            "and whole-book admission are required before exposure can be added."
        )


class BinanceChatGPTExecutor:
    """Retired live adapter; external ChatGPT signals cannot authorize orders.

    Keep this name temporarily for compatibility with imports, but make execution
    fail closed. Re-enable only after a first-class opportunity enters Apex's
    CognitiveBrain and shared portfolio/risk admission path.
    """

    def __init__(self) -> None:
        self.mode = os.getenv("BINANCE_EXECUTION_MODE", "PAPER").upper()
        self.live_enabled = os.getenv("BINANCE_LIVE_EXECUTION_ENABLED", "false").lower() == "true"
        # Do not instantiate a private trading client as a side effect of config.
        self.client = None

    def execute(self, signal: ChatGPTTradeSignal) -> ExecutionResult:
        raise BinanceExecutionError(
            "LIVE execution is disabled for external ChatGPT signals until the "
            "candidate is admitted by CognitiveBrain and the shared whole-book/risk path."
        )
