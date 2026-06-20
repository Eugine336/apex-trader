"""Deriv public tick-stream subscriber (Phase 3 — push ingestion).

Opens a **dedicated, unauthenticated** WebSocket to Deriv's public market-data
endpoint and subscribes to the ``ticks`` stream for a set of symbols, pushing
each update to a callback.  It deliberately uses its OWN socket — separate from
the OTP-authenticated order/account connection in ``DerivConnector`` — so a
streaming subscription can never interfere with the request/response framing
that order execution relies on (the connector's single account-scoped socket is
left untouched).

Safety / rollout posture:
  * Nothing constructs or starts this unless the host explicitly wires and
    enables it (review-and-validate-then-enable).
  * The message-parsing and symbol-mapping logic is pure and unit-tested; only
    the live connect/receive loop needs a real Deriv endpoint to validate.
  * If the stream errors or disconnects it simply reconnects with backoff — the
    existing polling path (``DerivConnector.get_price``) keeps working, so a
    stream failure degrades to current behavior rather than a tick outage.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Callable, Iterable, Optional

from loguru import logger

try:  # Network dependency is optional at import time (mirrors deriv_connector).
    import websockets  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - exercised only without the package
    websockets = None  # type: ignore[assignment]

_DEFAULT_WS_URL = "wss://ws.derivws.com/websockets/v3"
_RECONNECT_BASE_S = 1.0
_RECONNECT_MAX_S = 30.0

# callback(canonical_symbol, bid, ask, epoch_seconds)
TickCallback = Callable[[str, float, float, int], None]


class DerivTickStream:
    """Subscribe to Deriv's public ``ticks`` stream on a dedicated socket.

    Parameters
    ----------
    symbols:
        Canonical (app) symbols to subscribe to.
    on_tick:
        ``on_tick(symbol, bid, ask, epoch)`` — invoked for each parsed tick with
        the canonical symbol.  Exceptions are swallowed (best-effort delivery).
    app_id:
        Deriv app id (appended as the ``app_id`` query param).
    to_broker / to_canonical:
        Symbol mapping callables (canonical→broker for subscribe, broker→
        canonical for dispatch).  Default to identity.
    ws_url:
        Override the public endpoint (tests / staging).
    """

    def __init__(
        self,
        symbols: Iterable[str],
        on_tick: TickCallback,
        *,
        app_id: str = "",
        to_broker: Optional[Callable[[str], str]] = None,
        to_canonical: Optional[Callable[[str], str]] = None,
        ws_url: Optional[str] = None,
    ) -> None:
        self._symbols = [s for s in symbols if s]
        self._on_tick = on_tick
        self._app_id = app_id
        self._to_broker = to_broker or (lambda s: s)
        self._to_canonical = to_canonical or (lambda s: s)
        self._ws_url = ws_url or _DEFAULT_WS_URL
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ticks_received = 0

    # ── Pure helpers (unit-tested; no network) ───────────────────────────

    def _url(self) -> str:
        return f"{self._ws_url}?app_id={self._app_id}" if self._app_id else self._ws_url

    def _subscribe_payloads(self) -> list[dict]:
        return [{"ticks": self._to_broker(s), "subscribe": 1} for s in self._symbols]

    def _parse_tick(self, msg: dict) -> Optional[tuple[str, float, float, int]]:
        """Parse a Deriv tick frame to ``(canonical_symbol, bid, ask, epoch)``.

        Returns None for non-tick frames, malformed payloads, or non-positive
        prices.  The ``ticks`` stream carries bid/ask; ``quote`` is the
        fallback when one side is missing.
        """
        if not isinstance(msg, dict):
            return None
        tick = msg.get("tick")
        if not isinstance(tick, dict):
            return None
        broker_sym = tick.get("symbol")
        if not broker_sym:
            return None
        quote = tick.get("quote")
        bid = tick.get("bid", quote)
        ask = tick.get("ask", quote)
        try:
            bid_f = float(bid)
            ask_f = float(ask)
        except (TypeError, ValueError):
            return None
        if bid_f <= 0 or ask_f <= 0:
            return None
        try:
            epoch = int(tick.get("epoch", 0) or 0)
        except (TypeError, ValueError):
            epoch = 0
        canonical = self._to_canonical(broker_sym)
        if not canonical:
            return None
        return (canonical, bid_f, ask_f, epoch)

    def _dispatch(self, msg: dict) -> bool:
        """Parse + deliver one frame.  Returns True if a tick was delivered."""
        parsed = self._parse_tick(msg)
        if parsed is None:
            return False
        symbol, bid, ask, epoch = parsed
        try:
            self._on_tick(symbol, bid, ask, epoch)
        except Exception as exc:
            logger.debug("[deriv-tick-stream] on_tick callback failed: {}", exc)
            return False
        self._ticks_received += 1
        return True

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running or not self._symbols:
            return
        if websockets is None:
            logger.warning(
                "[deriv-tick-stream] websockets not installed — stream disabled",
            )
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="deriv-tick-stream",
        )
        self._thread.start()
        logger.info(
            "[deriv-tick-stream] started for {} symbols",
            len(self._symbols),
        )

    def stop(self) -> None:
        self._running = False
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        logger.info(
            "[deriv-tick-stream] stopped ({} ticks)",
            self._ticks_received,
        )

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._stream_loop())
        except Exception as exc:  # pragma: no cover - live loop only
            logger.warning("[deriv-tick-stream] loop ended: {}", exc)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _stream_loop(self) -> None:  # pragma: no cover - needs live socket
        attempt = 0
        while self._running:
            try:
                async with websockets.connect(
                    self._url(),
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                    open_timeout=30,
                ) as ws:
                    attempt = 0
                    for payload in self._subscribe_payloads():
                        await ws.send(json.dumps(payload))
                    while self._running:
                        raw = await ws.recv()
                        try:
                            msg = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(msg, dict) and msg.get("error"):
                            logger.debug(
                                "[deriv-tick-stream] error frame: {}",
                                msg["error"].get("message"),
                            )
                            continue
                        self._dispatch(msg)
            except Exception as exc:
                if not self._running:
                    break
                attempt += 1
                delay = min(_RECONNECT_BASE_S * attempt, _RECONNECT_MAX_S)
                logger.warning(
                    "[deriv-tick-stream] disconnected ({}); reconnect in {:.0f}s",
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

    @property
    def ticks_received(self) -> int:
        return self._ticks_received
