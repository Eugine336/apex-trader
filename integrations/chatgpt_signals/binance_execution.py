from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass
from pathlib import Path
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
    """Minimal Binance USD-M Futures signed REST client.

    Credentials are read only from environment variables. No withdrawal,
    transfer, or account-management endpoints are implemented.
    """

    BASE = os.getenv("BINANCE_FUTURES_BASE_URL", "https://fapi.binance.com")

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

    def position_risk(self, symbol: str) -> Any:
        return self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)

    def place_order(self, **params: Any) -> Any:
        return self._request("POST", "/fapi/v1/order", params, signed=True)


class BinanceChatGPTExecutor:
    """Executes ChatGPT signals. PAPER is default; LIVE requires explicit opt-in."""

    def __init__(self, journal: str = "runtime/chatgpt_execution_ledger.jsonl") -> None:
        self.mode = os.getenv("BINANCE_EXECUTION_MODE", "PAPER").upper()
        self.live_enabled = os.getenv("BINANCE_LIVE_EXECUTION_ENABLED", "false").lower() == "true"
        self.max_notional_usdt = float(os.getenv("BINANCE_MAX_NOTIONAL_USDT", "100"))
        self.journal = Path(journal)
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.seen: set[str] = set()
        if self.journal.exists():
            for line in self.journal.read_text(encoding="utf-8").splitlines():
                try:
                    self.seen.add(json.loads(line)["signal_id"])
                except Exception:
                    pass
        self.client = BinanceSignedClient() if self.mode == "LIVE" else None

    @staticmethod
    def _step_size(info: Any) -> Decimal:
        filters = info.get("symbols", [{}])[0].get("filters", [])
        for f in filters:
            if f.get("filterType") == "LOT_SIZE":
                return Decimal(str(f["stepSize"]))
        return Decimal("0.001")

    def _round_qty(self, qty: float, step: Decimal) -> float:
        q = Decimal(str(qty))
        rounded = (q / step).to_integral_value(rounding=ROUND_DOWN) * step
        return float(rounded)

    def _record(self, result: ExecutionResult) -> ExecutionResult:
        with self.journal.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result.__dict__, separators=(",", ":")) + "\n")
        self.seen.add(result.signal_id)
        return result

    def execute(self, signal: ChatGPTTradeSignal) -> ExecutionResult:
        if signal.mode != "PAPER":
            raise BinanceExecutionError("Only PAPER ChatGPT signals are accepted")
        if signal.expired():
            raise BinanceExecutionError(f"expired signal: {signal.signal_id}")
        if signal.signal_id in self.seen:
            raise BinanceExecutionError(f"duplicate signal: {signal.signal_id}")

        if self.mode != "LIVE":
            return self._record(ExecutionResult(
                signal_id=signal.signal_id, symbol=signal.symbol, side=signal.side,
                status="PAPER_ACCEPTED", order_id=None, fill_price=None,
                executed_qty=signal.quantity, mode="PAPER"
            ))

        if not self.live_enabled:
            raise BinanceExecutionError("LIVE mode selected but BINANCE_LIVE_EXECUTION_ENABLED is not true")
        assert self.client is not None

        if signal.stop_loss is None or signal.take_profit is None:
            raise BinanceExecutionError("LIVE execution requires stop_loss and take_profit")

        info = self.client.exchange_info(signal.symbol)
        symbol_info = info.get("symbols", [{}])[0]
        if not symbol_info:
            raise BinanceExecutionError(f"symbol unavailable: {signal.symbol}")
        step = self._step_size(info)
        qty = self._round_qty(signal.quantity, step)
        if qty <= 0:
            raise BinanceExecutionError("quantity rounds to zero")

        # Notional guard is checked against the signal's reference entry.
        reference = signal.stop_loss if signal.stop_loss else 0
        if reference <= 0:
            raise BinanceExecutionError("invalid signal pricing")
        # Caller should size quantity conservatively; this guard is intentionally
        # based on quantity * a reference price supplied via the signal extension.
        max_qty = float(Decimal(str(self.max_notional_usdt)) / Decimal(str(reference)))
        qty = self._round_qty(min(qty, max_qty), step)
        if qty <= 0:
            raise BinanceExecutionError("quantity exceeds configured notional limit")

        entry = self.client.place_order(
            symbol=signal.symbol,
            side=signal.side,
            type="MARKET",
            quantity=self._format_qty(qty),
            newOrderRespType="RESULT",
        )
        order_id = str(entry.get("orderId")) if entry.get("orderId") is not None else None
        fill_price = float(entry.get("avgPrice") or entry.get("price") or 0) or None
        executed_qty = float(entry.get("executedQty") or qty)

        exit_side = "SELL" if signal.side == "BUY" else "BUY"
        stop = self.client.place_order(
            symbol=signal.symbol, side=exit_side, type="STOP_MARKET",
            stopPrice=self._format_price(signal.stop_loss), closePosition="true",
            workingType="MARK_PRICE",
        )
        take = self.client.place_order(
            symbol=signal.symbol, side=exit_side, type="TAKE_PROFIT_MARKET",
            stopPrice=self._format_price(signal.take_profit), closePosition="true",
            workingType="MARK_PRICE",
        )

        return self._record(ExecutionResult(
            signal_id=signal.signal_id, symbol=signal.symbol, side=signal.side,
            status="FILLED", order_id=order_id, fill_price=fill_price,
            executed_qty=executed_qty,
            stop_order_id=str(stop.get("orderId")) if stop.get("orderId") is not None else None,
            take_profit_order_id=str(take.get("orderId")) if take.get("orderId") is not None else None,
            mode="LIVE"
        ))

    @staticmethod
    def _format_qty(value: float) -> str:
        return format(value, ".12f").rstrip("0").rstrip(".")

    @staticmethod
    def _format_price(value: float) -> str:
        return format(float(value), ".12f").rstrip("0").rstrip(".")
