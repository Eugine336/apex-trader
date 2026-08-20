from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class ChatGPTTradeSignal:
    signal_id: str
    symbol: str
    side: str
    order_type: str
    quantity: float
    stop_loss: float | None
    take_profit: float | None
    expires_at: datetime
    mode: str = "PAPER"
    source: str = "CHATGPT_OPPORTUNITY_ENGINE"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ChatGPTTradeSignal":
        required = ("signal_id", "symbol", "side", "order_type", "quantity", "expires_at")
        missing = [k for k in required if k not in raw]
        if missing:
            raise ValueError(f"missing required fields: {missing}")
        side = str(raw["side"]).upper()
        order_type = str(raw["order_type"]).upper()
        mode = str(raw.get("mode", "PAPER")).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if order_type not in {"MARKET"}:
            raise ValueError("only MARKET signals are enabled in the initial adapter")
        if mode != "PAPER":
            raise ValueError("initial ChatGPT executor is PAPER-only")
        expires = str(raw["expires_at"])
        if expires.endswith("Z"):
            expires = expires[:-1] + "+00:00"
        dt = datetime.fromisoformat(expires)
        if dt.tzinfo is None:
            raise ValueError("expires_at must include timezone")
        qty = float(raw["quantity"])
        if qty <= 0:
            raise ValueError("quantity must be positive")
        return cls(
            signal_id=str(raw["signal_id"]),
            symbol=str(raw["symbol"]).upper(),
            side=side,
            order_type=order_type,
            quantity=qty,
            stop_loss=float(raw["stop_loss"]) if raw.get("stop_loss") is not None else None,
            take_profit=float(raw["take_profit"]) if raw.get("take_profit") is not None else None,
            expires_at=dt,
            mode=mode,
            source=str(raw.get("source", "CHATGPT_OPPORTUNITY_ENGINE")),
        )

    def expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now >= self.expires_at.astimezone(timezone.utc)
