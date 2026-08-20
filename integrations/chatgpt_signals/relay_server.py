from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .binance_paper_executor import BinancePaperExecutor
from .schema import ChatGPTTradeSignal

TOKEN = os.getenv("CHATGPT_RELAY_TOKEN", "")
HOST = os.getenv("CHATGPT_RELAY_HOST", "127.0.0.1")
PORT = int(os.getenv("CHATGPT_RELAY_PORT", "8790"))

app = FastAPI(title="APEX ChatGPT Signal Relay", version="1.0.0")
executor = BinancePaperExecutor()

class SignalEnvelope(BaseModel):
    signal: dict[str, Any] = Field(...)


def authorize(token: str | None) -> None:
    if not TOKEN:
        raise HTTPException(503, "CHATGPT_RELAY_TOKEN is not configured")
    if token != TOKEN:
        raise HTTPException(401, "invalid relay token")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "mode": "PAPER", "read_only_binance": True}


@app.post("/v1/signals")
def receive_signal(envelope: SignalEnvelope, x_apex_relay_token: str | None = Header(default=None)):
    authorize(x_apex_relay_token)
    signal = ChatGPTTradeSignal.from_dict(envelope.signal)
    fill = executor.execute(signal)
    return {"accepted": True, "fill": fill.__dict__}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
