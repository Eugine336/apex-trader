from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .executor import ChatGPTExecutor
from .schema import ChatGPTTradeSignal

TOKEN = os.getenv("CHATGPT_RELAY_TOKEN", "")
HOST = os.getenv("CHATGPT_RELAY_HOST", "127.0.0.1")
PORT = int(os.getenv("CHATGPT_RELAY_PORT", "8790"))
MODE = os.getenv("BINANCE_EXECUTION_MODE", "PAPER").upper()

app = FastAPI(title="APEX ChatGPT Signal Relay", version="2.0.0")
executor = ChatGPTExecutor()


class SignalEnvelope(BaseModel):
    signal: dict[str, Any] = Field(...)


def authorize(token: str | None) -> None:
    if not TOKEN:
        raise HTTPException(503, "CHATGPT_RELAY_TOKEN is not configured")
    if token != TOKEN:
        raise HTTPException(401, "invalid relay token")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "execution_mode": MODE,
        "live_enabled": os.getenv("BINANCE_LIVE_EXECUTION_ENABLED", "false").lower() == "true",
        "binance_private_api": MODE == "LIVE",
    }


@app.post("/v1/signals")
def receive_signal(envelope: SignalEnvelope, x_apex_relay_token: str | None = Header(default=None)):
    authorize(x_apex_relay_token)
    signal = ChatGPTTradeSignal.from_dict(envelope.signal)
    if MODE == "LIVE" and signal.mode != "LIVE":
        raise HTTPException(400, "LIVE executor requires a LIVE signal")
    if MODE != "LIVE" and signal.mode != "PAPER":
        raise HTTPException(400, "PAPER executor requires a PAPER signal")
    try:
        result = executor.execute(signal)
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"accepted": True, "result": result.__dict__}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
