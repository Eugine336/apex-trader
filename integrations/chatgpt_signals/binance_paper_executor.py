from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.request import Request, urlopen

from .schema import ChatGPTTradeSignal


@dataclass
class PaperFill:
    signal_id: str
    symbol: str
    side: str
    quantity: float
    fill_price: float
    stop_loss: float | None
    take_profit: float | None
    timestamp_ms: int
    source: str
    mode: str = "PAPER"


class BinancePaperExecutor:
    """Paper-only executor using Binance public Futures prices.

    This adapter deliberately has no private Binance endpoints and no order
    placement capability. It turns a valid ChatGPT signal into a deterministic
    simulated fill at the current Binance Futures book price.
    """

    BASE = "https://fapi.binance.com"

    def __init__(self, journal: str = "runtime/chatgpt_signal_fills.jsonl") -> None:
        self.journal = Path(journal)
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.seen: set[str] = set()
        if self.journal.exists():
            for line in self.journal.read_text(encoding="utf-8").splitlines():
                try:
                    self.seen.add(json.loads(line)["signal_id"])
                except Exception:
                    continue

    def _book(self, symbol: str) -> tuple[float, float]:
        url = f"{self.BASE}/fapi/v1/ticker/bookTicker?symbol={symbol}"
        req = Request(url, headers={"User-Agent": "APEX-ChatGPT-PaperExecutor/1.0"})
        with urlopen(req, timeout=5) as response:
            raw = json.loads(response.read().decode("utf-8"))
        return float(raw["bidPrice"]), float(raw["askPrice"])

    def execute(self, signal: ChatGPTTradeSignal) -> PaperFill:
        if signal.mode != "PAPER":
            raise RuntimeError("ChatGPT signal executor is PAPER-only")
        if signal.expired():
            raise RuntimeError(f"signal expired: {signal.signal_id}")
        if signal.signal_id in self.seen:
            raise RuntimeError(f"duplicate signal: {signal.signal_id}")

        bid, ask = self._book(signal.symbol)
        fill = ask if signal.side == "BUY" else bid
        result = PaperFill(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=signal.side,
            quantity=signal.quantity,
            fill_price=fill,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            timestamp_ms=int(time.time() * 1000),
            source=signal.source,
        )
        with self.journal.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(result), separators=(",", ":")) + "\n")
        self.seen.add(signal.signal_id)
        return result
