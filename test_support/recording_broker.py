"""Recording broker fake for deterministic, non-live execution tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class FakeCloseResult:
    success: bool = True
    error: str = ""
    close_price: float = 1.0850
    lots_closed: float = 0.10
    pnl: float = 12.50


@dataclass
class FakeOpenResult:
    success: bool = True
    error: Optional[str] = None
    order_id: str = "99999"


class RecordingFakeBroker:
    """Minimal ActionExecutor BrokerPort fake that records broker dispatches.

    No method contacts a broker. Tests can configure return values or exceptions
    while inspecting both operation-specific calls and the ordered call ledger.
    """

    def __init__(self) -> None:
        self.modify_calls: list[dict[str, Any]] = []
        self.close_calls: list[dict[str, Any]] = []
        self.open_calls: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.modify_return: bool = True
        self.close_return: FakeCloseResult = FakeCloseResult()
        self.open_return: Any = FakeOpenResult()
        self.raise_on_modify: Optional[Exception] = None
        self.raise_on_close: Optional[Exception] = None
        self.raise_on_open: Optional[Exception] = None
        self.call_count = 0

    def modify_trade(
        self,
        order_id: str,
        platform: str,
        new_sl: Optional[float] = None,
        new_tp: Optional[float] = None,
    ) -> bool:
        self.call_count += 1
        self.calls.append({
            "operation": "modify",
            "order_id": order_id,
            "platform": platform,
            "new_sl": new_sl,
            "new_tp": new_tp,
        })
        if self.raise_on_modify:
            raise self.raise_on_modify
        self.modify_calls.append({
            "order_id": order_id,
            "platform": platform,
            "new_sl": new_sl,
            "new_tp": new_tp,
        })
        return self.modify_return

    def close_trade(
        self,
        order_id: str,
        platform: str,
        lots: Optional[float] = None,
    ) -> FakeCloseResult:
        self.call_count += 1
        self.calls.append({
            "operation": "close",
            "order_id": order_id,
            "platform": platform,
            "lots": lots,
        })
        if self.raise_on_close:
            raise self.raise_on_close
        self.close_calls.append({
            "order_id": order_id,
            "platform": platform,
            "lots": lots,
        })
        return self.close_return

    def get_symbol_spec(self, symbol: str) -> dict[str, Any]:
        """Verified synthetic constraints for unit tests only; never live broker data."""
        return {
            "symbol": symbol,
            "source": "test_support.recording_broker",
            "source_verified": True,
            "specification_verified": True,
            "trade_mode": 4,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
            "point": 0.00001,
            "trade_stops_level": 0,
            "trade_tick_value": 1.0,
            "trade_tick_size": 0.00001,
        }

    def execute_entry(
        self,
        symbol: str,
        direction: str,
        lots: float,
        sl: float,
        tp: float,
        comment: str = "",
        stake_usd: Optional[float] = None,
        idempotency_key: str = "",
    ) -> Any:
        self.call_count += 1
        self.calls.append({
            "operation": "open",
            "symbol": symbol,
            "direction": direction,
            "lots": lots,
            "sl": sl,
            "tp": tp,
            "comment": comment,
            "stake_usd": stake_usd,
            "idempotency_key": idempotency_key,
        })
        if self.raise_on_open:
            raise self.raise_on_open
        self.open_calls.append({
            "symbol": symbol,
            "direction": direction,
            "lots": lots,
            "sl": sl,
            "tp": tp,
        })
        return self.open_return


# Backward-compatible name used by existing executor test helpers.
FakeBroker = RecordingFakeBroker
