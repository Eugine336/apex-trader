"""Regression tests for Deriv auto-reconnect robustness.

Live symptom: when the Deriv WebSocket expired/dropped it "didn't reboot on
its own". Two reconnect paths exist — the reactive one (``_send_raw`` on a
dropped socket) and the proactive one (PlatformManager watchdog → ``connect``).
Both scheduled ``_connect_async`` on the same event loop with no mutual
exclusion, so they could interleave and each open a socket, leaking one and
eventually tripping Deriv's 5-concurrent-connection cap — after which every
further reconnect was refused.

These tests pin the fix:
  * ``_connect_async`` is idempotent — it never opens a second socket when one
    is already live.
  * Concurrent (re)connects collapse to a single ``_do_connect``.
  * ``_reconnect`` honours the configured attempt count and always clears the
    ``_reconnecting`` flag.
"""

import asyncio

from platforms.deriv.deriv_connector import DerivConnector


class _FakeOpenWS:
    """Minimal websocket double that reports itself as OPEN.

    Has no ``.state`` attribute, so ``_ws_is_open`` falls back to ``.open``
    (the path taken when the websockets State enum is unavailable).
    """

    open = True

    async def close(self):  # pragma: no cover - not exercised here
        self.open = False


def _bare_connector() -> DerivConnector:
    """Build a connector with only the reconnect machinery wired (no real loop)."""
    c = DerivConnector.__new__(DerivConnector)
    c._connect_lock = asyncio.Lock()
    c._connected = False
    c._authorized = False
    c._reconnecting = False
    c._ws = None
    c._max_reconnect_attempts = 3
    c._reconnect_base_delay = 0.0  # no real backoff sleeps in the test
    c._token_expires_at = 9_999_999_999.0  # far future — token "valid"
    return c


def test_connect_async_idempotent_when_already_open():
    """A live socket short-circuits _connect_async without re-opening."""
    c = _bare_connector()
    c._connected = True
    c._ws = _FakeOpenWS()

    calls = {"n": 0}

    async def _fake_do_connect():
        calls["n"] += 1
        return True

    c._do_connect = _fake_do_connect

    assert asyncio.run(c._connect_async()) is True
    assert calls["n"] == 0  # never opened a duplicate socket


def test_concurrent_connect_opens_single_socket():
    """Two concurrent _connect_async calls collapse to one _do_connect."""
    c = _bare_connector()
    calls = {"n": 0}

    async def _fake_do_connect():
        calls["n"] += 1
        await asyncio.sleep(0.02)  # widen the race window
        c._connected = True
        c._ws = _FakeOpenWS()
        return True

    c._do_connect = _fake_do_connect

    async def _drive():
        return await asyncio.gather(c._connect_async(), c._connect_async())

    results = asyncio.run(_drive())
    assert results == [True, True]
    # The second caller saw the socket the first opened and skipped _do_connect.
    assert calls["n"] == 1


def test_reconnect_honours_config_attempts_and_clears_flag():
    """_reconnect retries exactly _max_reconnect_attempts times then gives up."""
    c = _bare_connector()
    c._max_reconnect_attempts = 4
    calls = {"n": 0}

    async def _fake_do_connect():
        calls["n"] += 1
        return False  # never recovers

    c._do_connect = _fake_do_connect

    ok = asyncio.run(c._reconnect())
    assert ok is False
    assert calls["n"] == 4
    # The reconnecting flag must always be reset so requests aren't blocked.
    assert c._reconnecting is False


def test_reconnect_stops_early_when_token_expired():
    """An expired access token aborts the retry loop after the first attempt."""
    c = _bare_connector()
    c._max_reconnect_attempts = 5
    c._token_expires_at = 0.0  # already expired
    calls = {"n": 0}

    async def _fake_do_connect():
        calls["n"] += 1
        return False

    c._do_connect = _fake_do_connect

    ok = asyncio.run(c._reconnect())
    assert ok is False
    # First attempt runs, then the expired-token guard aborts further retries.
    assert calls["n"] == 1
    assert c._reconnecting is False
