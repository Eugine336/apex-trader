from __future__ import annotations

import os
from .binance_paper_executor import BinancePaperExecutor
from .binance_execution import BinanceChatGPTExecutor, ExecutionResult
from .schema import ChatGPTTradeSignal


class ChatGPTExecutor:
    """Single execution boundary for ChatGPT-originated Binance signals.

    PAPER is the default. LIVE requires both BINANCE_EXECUTION_MODE=LIVE and
    BINANCE_LIVE_EXECUTION_ENABLED=true, plus Binance API credentials.
    """

    def __init__(self) -> None:
        self.mode = os.getenv("BINANCE_EXECUTION_MODE", "PAPER").upper()
        self.paper = BinancePaperExecutor()
        self.live = BinanceChatGPTExecutor() if self.mode == "LIVE" else None

    def execute(self, signal: ChatGPTTradeSignal):
        if self.mode == "LIVE":
            return self.live.execute(signal)  # type: ignore[union-attr]
        return self.paper.execute(signal)
