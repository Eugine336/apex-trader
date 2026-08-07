"""Regression tests for Deriv websocket response correlation by ``req_id``.

The live failure: a documented-correct ``proposal`` request (which carries
``symbol``) appeared to fail with the buy-by-parameters error
'Properties not allowed: symbol'. Root cause — ``_send`` sent a request tagged
with a ``req_id`` but then blindly read the *next* websocket frame, so a stale
or out-of-band frame (e.g. a previous failed buy's error, or a ``subscribe``
stream tick) was mis-attributed to the new request. ``_send`` now discards
uncorrelated frames until the matching ``req_id`` arrives.
"""

import asyncio
import json

from platforms.deriv.deriv_connector import DerivConnector


class _FakeWS:
    """Async websocket double that replays a fixed list of raw frames."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        if not self._frames:
            # Simulate a quiet socket so the request times out rather than
            # hanging forever if no correlated frame ever arrives.
            await asyncio.sleep(0.01)
            raise AssertionError("recv called with no frames left")
        return self._frames.pop(0)


def _make_connector(frames):
    conn = DerivConnector.__new__(DerivConnector)
    conn._req_id = 0
    conn._lock = asyncio.Lock()
    conn._ws = _FakeWS(frames)
    return conn


def test_send_skips_stale_frames_and_returns_correlated_response():
    # req_id will be 1. Two stale frames precede the correlated one.
    frames = [
        json.dumps({"req_id": 999, "error": {"message": "Properties not allowed: symbol"}}),
        json.dumps({"msg_type": "tick", "tick": {"quote": 123.0}}),  # no req_id
        json.dumps({"req_id": 1, "proposal": {"id": "PID-1", "ask_price": 47.0}}),
    ]
    conn = _make_connector(frames)
    resp = asyncio.run(conn._send({"proposal": 1, "symbol": "1HZ50V"}))
    assert resp.get("proposal", {}).get("id") == "PID-1"
    # The request was tagged with the correlation id.
    assert json.loads(conn._ws.sent[0])["req_id"] == 1


def test_send_returns_first_matching_when_no_stale_frames():
    frames = [json.dumps({"req_id": 1, "buy": {"contract_id": "C-1"}})]
    conn = _make_connector(frames)
    resp = asyncio.run(conn._send({"buy": "PID-1", "price": 47.0}))
    assert resp.get("buy", {}).get("contract_id") == "C-1"
