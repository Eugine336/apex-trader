from __future__ import annotations

import os
from .binance_paper_executor import BinancePaperExecutor
from .binance_execution import BinanceExecutionError
from .schema import ChatGPTTradeSignal


class ChatGPTExecutor:
    """Single execution boundary for ChatGPT-originated Binance signals.

    PAPER is the only supported mode. External ChatGPT signals are inputs, not
    an independent authority to create Apex exposure. LIVE stays blocked until
    a first-class CognitiveBrain opportunity uses the common admission path.
    """

    def __init__(self) -> None:
        self.mode = os.getenv("BINANCE_EXECUTION_MODE", "PAPER").upper()
        self.paper = BinancePaperExecutor()

    def execute(self, signal: ChatGPTTradeSignal):
        if self.mode != "PAPER" or signal.mode != "PAPER":
            raise BinanceExecutionError(
                "ChatGPT signal relay is paper-only: external signals cannot "
                "authorize Apex exposure outside CognitiveBrain admission."
            )
        return self.paper.execute(signal)
