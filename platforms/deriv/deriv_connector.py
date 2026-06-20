"""
APEX TRADER — Deriv WebSocket Connector
Connects to Deriv via the official WebSocket API.
Handles synthetics (V75, Boom/Crash) 24/7 and Forex on Deriv.
"""

import os
import asyncio
import json
import threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd
from loguru import logger

from brain.symbol_mapper import SymbolMapper
from config import get_pip_size
from persistence.deriv_position_store import DerivPositionStore
from platforms.base_connector import (
    AccountInfo,
    BaseConnector,
    CloseResult,
    DealCloseInfo,
    OrderResult,
    PositionInfo,
    TickData,
)

try:
    import websockets  # type: ignore[import-untyped]
    import websockets.client  # type: ignore[import-untyped]

    _WS_AVAILABLE = True
except ImportError:
    websockets = None  # type: ignore[assignment]
    _WS_AVAILABLE = False

try:
    import aiohttp  # type: ignore[import-untyped]

    _AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    _AIOHTTP_AVAILABLE = False

# ── New Deriv API (June 2026 migration) ──────────────────────────────────────
# Authentication moved from the legacy `{"authorize": token}` WebSocket message
# to an OAuth2 access-token + REST OTP flow:
#   1. GET  /trading/v1/options/accounts        (Bearer access_token) -> accountId
#   2. POST /trading/v1/options/accounts/{id}/otp                     -> pre-authed WS URL
#   3. open WebSocket using that OTP URL (no authorize message needed)
# OTPs are single-use, so a fresh one must be fetched before each (re)connect.
_DERIV_REST_BASE = "https://api.derivws.com"
_DERIV_ACCOUNTS_URL = f"{_DERIV_REST_BASE}/trading/v1/options/accounts"
_DERIV_OTP_URL = f"{_DERIV_REST_BASE}/trading/v1/options/accounts/{{account_id}}/otp"
_DERIV_HEALTH_URL = f"{_DERIV_REST_BASE}/v1/health"

_REST_TIMEOUT = 20
# Refresh/expiry warning window: warn when the access token is within this many
# seconds of expiry, since a reconnect after expiry cannot self-recover.
_TOKEN_EXPIRY_WARN_SECONDS = 300

# Proactively refresh the access token once this fraction of its TTL has
# elapsed (i.e. while there is still ~20% of life left), so a refresh failure
# is surfaced before the token actually dies and a reconnect becomes impossible.
_TOKEN_REFRESH_FRACTION = 0.8

# Background token-monitor poll interval (seconds).
_TOKEN_MONITOR_INTERVAL = 30.0

_GRANULARITY_MAP: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}

_RECONNECT_DELAY = 5
_REQUEST_TIMEOUT = 20
_MAX_RECONNECT_ATTEMPTS = 10
_PING_INTERVAL = 20       # send keepalive ping every 20 s
_PING_TIMEOUT  = 10       # fail if pong not received within 10 s

# Minimum spacing between ``ticks_history`` requests — Deriv rate-limits candle/
# tick history to ~3 req/s. The spacing is enforced by reserving a send slot
# (see _reserve_history_slot) so the wait happens OUTSIDE any lock and unrelated
# Deriv calls (price, balance, buy) are never serialised behind a history wait.
_HISTORY_MIN_INTERVAL = 0.5

# Deriv's `balance` endpoint is strictly rate-limited. Coalesce bursts of
# balance/account-info requests within this TTL into a single API call, and on
# a rate-limit (or other) error serve the last good value up to the max-stale
# age rather than failing every balance-dependent path (entry sizing, margin).
_BALANCE_CACHE_TTL = 3.0   # seconds — within this window, serve cache, no API hit
_BALANCE_MAX_STALE = 90.0  # seconds — serve cached balance through an error storm

# Deriv multiplier contracts auto-liquidate at a loss equal to the stake, so the
# limit_order.stop_loss (in account currency) can never exceed the stake. Keep
# stake × multiplier × sl_pct strictly below the stake by this safety fraction
# (leaving headroom for the broker's deal commission) or Deriv rejects the order
# with 'Input validation failed: parameters'.
_SL_STAKE_SAFETY = 0.90

# Conservative multiplier set used only when neither runtime discovery nor the
# static config supplies an accepted list for a symbol. These values are
# commonly valid across Deriv synthetic indices; the previous list contained
# 80, which Deriv rejects for the 1s volatility indices (1HZ..V). A wrong guess
# here self-corrects on the first order: Deriv returns the real accepted list in
# error.details, which place_order parses and snaps to (see _error_text).
_FALLBACK_ACCEPTED_MULTIPLIERS = [100, 200, 300, 400, 500]


class DerivConnector(BaseConnector):
    """Deriv WebSocket platform connector."""

    def __init__(
        self,
        client_id: str = "",
        access_token: str = "",
        account_type: str = "demo",
        token_expires_in: float = 3600.0,
        app_id: str = "",
        max_tick_age_seconds: float = 120.0,
        token_refresh_callback: Optional[Callable[[], tuple[str, float]]] = None,
        position_store: Optional[DerivPositionStore] = None,
    ):
        # New API: OAuth2 access token + REST OTP flow.
        # client_id     — OAuth2 client id (from the Deriv Developer Dashboard)
        # access_token  — short-lived OAuth2 access token (ory_at_...)
        # account_type  — "demo" or "real" (selects which account to use)
        # app_id        — value for the optional Deriv-App-ID REST header
        self._client_id = client_id
        self._access_token = access_token
        self._account_type = (account_type or "demo").strip().lower()
        self._app_id = app_id
        # Track token expiry so we can warn before a reconnect would fail.
        try:
            self._token_ttl: float = float(token_expires_in)
        except (TypeError, ValueError):
            self._token_ttl = 3600.0
        if self._token_ttl <= 0:
            self._token_ttl = 3600.0
        self._token_issued_at: float = _time.time()
        self._token_expires_at: float = self._token_issued_at + self._token_ttl
        self._token_expiry_warned = False
        # Optional callable returning (new_access_token, expires_in_seconds).
        # Deriv's OAuth2 access tokens are short-lived and this flow has no
        # built-in refresh-token grant, so rotation is delegated to the host
        # (e.g. a credential broker). When absent we can only warn + wind down.
        self._token_refresh_callback = token_refresh_callback
        self._token_lock = threading.Lock()
        self._token_monitor_stop = threading.Event()
        self._token_monitor_thread: Optional[threading.Thread] = None

        self._max_tick_age_seconds = float(
            os.getenv("MAX_TICK_AGE_SECONDS", str(max_tick_age_seconds))
        )
        self._ws: Any = None
        self._connected = False
        self._reconnecting = False
        self._authorized = False
        self._account_id: str = ""
        self._req_id = 0
        self._positions: dict[str, dict] = {}
        # Crash-safe persistence: Deriv's broker portfolio does not echo back our
        # internal SL/TP/idem-key, so without this the metadata is lost on
        # restart. Load any persisted contracts so they are managed immediately.
        try:
            self._store: Optional[DerivPositionStore] = position_store or DerivPositionStore()
            persisted = self._store.load_all_positions()
            if persisted:
                self._positions.update(persisted)
                logger.info(
                    "DerivConnector restored {} persisted position(s) from store",
                    len(persisted),
                )
        except Exception as exc:
            logger.error("DerivPositionStore init failed — running without Deriv persistence: {}", exc)
            self._store = None
        self._mapper = SymbolMapper("deriv")

        self._discovered_multipliers: dict[str, list[int]] = {}

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="deriv-ws-loop",
        )
        self._loop_thread.start()
        # asyncio.Lock must be created on self._loop (not the main thread's loop).
        # Creating it in __init__ on the wrong loop causes silent failures in
        # _discover_multipliers, leaving _discovered_multipliers empty and forcing
        # fallback to hardcoded multipliers (often 1000) that Deriv rejects.
        self._lock: asyncio.Lock = asyncio.run_coroutine_threadsafe(
            self._make_lock(), self._loop
        ).result(timeout=5)
        self._thread_lock = threading.Lock()
        self._last_history_request: float = 0.0
        # ticks_history rate-limit gate. ``_next_history_at`` is the monotonic
        # time the next history send is permitted; threads reserve spaced slots
        # under ``_history_lock`` then sleep OUTSIDE the lock, so a history wait
        # never blocks unrelated Deriv calls.
        self._history_lock = threading.Lock()
        self._next_history_at: float = 0.0
        # Balance/account-info cache (see _BALANCE_CACHE_TTL). A dedicated lock
        # serialises balance fetches so a burst collapses to one API call.
        self._balance_lock = threading.Lock()
        self._acct_cache: Optional[AccountInfo] = None
        self._acct_cache_ts: float = 0.0

    # ── Connection ───────────────────────────────────────────────────────

    def connect(self) -> bool:
        if not _WS_AVAILABLE:
            logger.error("websockets package not installed")
            return False
        if not _AIOHTTP_AVAILABLE:
            logger.error("aiohttp package not installed — required for Deriv OAuth2/OTP flow")
            return False
        if not self._access_token:
            logger.error("Deriv access_token not provided (set DERIV_ACCESS_TOKEN)")
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(self._connect_async(), self._loop)
            return future.result(timeout=60)
        except Exception as exc:
            logger.error("Deriv connect error: {}", exc)
            return False

    async def _connect_async(self) -> bool:
        # ── New API flow ────────────────────────────────────────────────────
        # 1. Verify the access token has not expired (a reconnect cannot
        #    self-recover without a fresh token).
        # 2. Resolve our accountId via REST (cached after the first success).
        # 3. Fetch a fresh, single-use OTP WebSocket URL via REST.
        # 4. Open the WebSocket — it is pre-authenticated, so no authorize
        #    message is sent.
        if not self._check_token_valid():
            return False

        if not self._account_id:
            account_id = await self._get_account_id()
            if not account_id:
                return False
            self._account_id = account_id

        ws_url = await self._get_otp_ws_url(self._account_id)
        if not ws_url:
            return False

        try:
            self._ws = await websockets.connect(
                ws_url,
                ping_interval=_PING_INTERVAL,
                ping_timeout=_PING_TIMEOUT,
                close_timeout=10,
                open_timeout=30,
            )
        except Exception as exc:
            logger.error("Deriv WS connect failed: {}", exc)
            return False

        # The OTP-authenticated socket needs no authorize message.
        self._authorized = True
        self._connected = True
        logger.info(
            "Deriv connected — account {} ({}), OTP-authenticated WebSocket",
            self._account_id,
            self._account_type,
        )

        await self._discover_multipliers()
        self._start_token_monitor()
        return True

    # ── REST auth helpers (new API) ──────────────────────────────────────

    def _check_token_valid(self) -> bool:
        """Return True if the access token is still usable.

        Logs a WARNING when the token is approaching expiry and a CRITICAL
        error once it has expired — at that point the bot cannot recover the
        WebSocket without a freshly issued access token.
        """
        if not self._access_token:
            logger.critical("Deriv access_token missing — cannot authenticate")
            return False
        remaining = self._token_expires_at - _time.time()
        if remaining <= 0:
            logger.critical(
                "Deriv access_token has EXPIRED ({}s ago) — reconnect cannot "
                "succeed until DERIV_ACCESS_TOKEN is refreshed",
                int(abs(remaining)),
            )
            return False
        if remaining <= _TOKEN_EXPIRY_WARN_SECONDS:
            if not self._token_expiry_warned:
                logger.warning(
                    "Deriv access_token expires in {}s — refresh DERIV_ACCESS_TOKEN soon",
                    int(remaining),
                )
                self._token_expiry_warned = True
        else:
            self._token_expiry_warned = False
        return True

    # ── Token rotation / proactive refresh ───────────────────────────────

    def set_access_token(self, token: str, expires_in: float = 3600.0) -> None:
        """Rotate the access token (e.g. after an out-of-band refresh).

        Resets the issued/expiry clock so the proactive-refresh and warning
        windows track the new token.
        """
        if not token:
            return
        with self._token_lock:
            self._access_token = token
            try:
                self._token_ttl = float(expires_in)
            except (TypeError, ValueError):
                self._token_ttl = 3600.0
            if self._token_ttl <= 0:
                self._token_ttl = 3600.0
            self._token_issued_at = _time.time()
            self._token_expires_at = self._token_issued_at + self._token_ttl
            self._token_expiry_warned = False
        logger.info("Deriv access_token rotated — valid for ~{}s", int(self._token_ttl))

    def token_expired(self) -> bool:
        """True once the access token has expired (reconnect impossible)."""
        return (self._token_expires_at - _time.time()) <= 0

    def _maybe_refresh_token(self) -> bool:
        """Proactively refresh the token once ~80% of its TTL has elapsed.

        Returns True if a refresh was attempted and succeeded. When no refresh
        callback is configured this is a no-op (the caller still warns/winds
        down via _check_token_valid / the monitor loop).
        """
        if self._token_refresh_callback is None:
            return False
        elapsed = _time.time() - self._token_issued_at
        if elapsed < self._token_ttl * _TOKEN_REFRESH_FRACTION:
            return False
        try:
            new_token, expires_in = self._token_refresh_callback()
        except Exception as exc:
            logger.critical(
                "Deriv token refresh callback FAILED ({}s of {}s TTL elapsed) — "
                "connection will halt at expiry unless DERIV_ACCESS_TOKEN is rotated: {}",
                int(elapsed), int(self._token_ttl), exc,
            )
            return False
        if not new_token:
            logger.critical("Deriv token refresh returned an empty token — keeping current token")
            return False
        self.set_access_token(new_token, expires_in)
        return True

    def _token_monitor_loop(self) -> None:
        """Background watchdog: refresh before expiry, escalate on expiry.

        Refreshes proactively when a callback is available; otherwise logs a
        CRITICAL once the token has expired while contracts are still open so
        the operator knows those positions must be wound down manually.
        """
        winddown_warned = False
        while not self._token_monitor_stop.wait(_TOKEN_MONITOR_INTERVAL):
            try:
                if self._maybe_refresh_token():
                    winddown_warned = False
                    continue
                if self.token_expired():
                    open_count = len(self._positions)
                    if open_count and not winddown_warned:
                        logger.critical(
                            "🚨 Deriv access_token EXPIRED with {} open contract(s) — "
                            "cannot manage them until DERIV_ACCESS_TOKEN is refreshed. "
                            "Wind down Deriv exposure immediately.",
                            open_count,
                        )
                        winddown_warned = True
                else:
                    # Re-run the warn-window check so operators get an early heads-up.
                    self._check_token_valid()
                    winddown_warned = False
            except Exception as exc:
                logger.debug("[deriv] token monitor iteration failed: {}", exc)

    def _start_token_monitor(self) -> None:
        if self._token_monitor_thread is not None and self._token_monitor_thread.is_alive():
            return
        self._token_monitor_stop.clear()
        self._token_monitor_thread = threading.Thread(
            target=self._token_monitor_loop,
            daemon=True,
            name="deriv-token-monitor",
        )
        self._token_monitor_thread.start()

    def _stop_token_monitor(self) -> None:
        self._token_monitor_stop.set()

    def _rest_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._app_id:
            headers["Deriv-App-ID"] = self._app_id
        return headers

    async def _rest_request(
        self, method: str, url: str, *, auth: bool = True
    ) -> Optional[dict]:
        """Perform a REST call against the Deriv API.

        Returns the parsed JSON dict on success, or None on any failure.
        A 401 is logged as CRITICAL because it means the access token is no
        longer valid and the bot cannot self-recover.
        """
        headers = self._rest_headers() if auth else {"Accept": "application/json"}
        timeout = aiohttp.ClientTimeout(total=_REST_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, headers=headers) as resp:
                    status = resp.status
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        body = {"raw": await resp.text()}
                    if status == 401:
                        logger.critical(
                            "Deriv REST {} {} → 401 Unauthorized. Access token is "
                            "invalid/expired — refresh DERIV_ACCESS_TOKEN.",
                            method, url,
                        )
                        return None
                    if status == 429:
                        logger.warning(
                            "Deriv REST {} {} → 429 rate limited (limit ~60 req/min)",
                            method, url,
                        )
                        return None
                    if status >= 400:
                        logger.error(
                            "Deriv REST {} {} → HTTP {}: {}",
                            method, url, status, body,
                        )
                        return None
                    return body if isinstance(body, dict) else {"data": body}
        except Exception as exc:
            logger.error("Deriv REST {} {} failed: {}", method, url, exc)
            return None

    async def _check_health(self) -> bool:
        """Best-effort health probe. Returns True unless the API explicitly
        reports unhealthy; never blocks connection on a missing/ambiguous
        response."""
        body = await self._rest_request("GET", _DERIV_HEALTH_URL, auth=False)
        if body is None:
            return True  # probe failed — don't block, the connect will surface real errors
        status = str(body.get("status", body.get("health", ""))).lower()
        if status and status not in ("ok", "up", "healthy", "pass", "available"):
            logger.error("Deriv health check reports unhealthy: {}", body)
            return False
        return True

    @staticmethod
    def _extract_accounts(body: dict) -> list[dict]:
        """Normalise the various shapes the accounts payload may take."""
        if not isinstance(body, dict):
            return []
        for key in ("accounts", "data", "items", "results"):
            val = body.get(key)
            if isinstance(val, list):
                return [a for a in val if isinstance(a, dict)]
        # Single-account payload returned directly.
        if any(k in body for k in ("accountId", "account_id", "loginid")):
            return [body]
        return []

    async def _get_account_id(self) -> str:
        """Resolve the accountId for the configured account type via REST."""
        body = await self._rest_request("GET", _DERIV_ACCOUNTS_URL)
        if body is None:
            logger.error("Deriv accounts lookup failed — cannot resolve accountId")
            return ""
        accounts = self._extract_accounts(body)
        if not accounts:
            logger.error("Deriv accounts response contained no accounts: {}", body)
            return ""

        def _acct_id(acct: dict) -> str:
            return str(
                acct.get("accountId")
                or acct.get("account_id")
                or acct.get("loginid")
                or ""
            ).strip()

        def _acct_type(acct: dict) -> str:
            raw = (
                acct.get("type")
                or acct.get("account_type")
                or acct.get("category")
                or ""
            )
            is_virtual = acct.get("is_virtual")
            if is_virtual in (1, True):
                return "demo"
            if is_virtual in (0, False) and not raw:
                return "real"
            return str(raw).strip().lower()

        # Prefer an account whose type matches the configured account_type.
        for acct in accounts:
            atype = _acct_type(acct)
            if self._account_type == "demo" and atype in ("demo", "virtual"):
                aid = _acct_id(acct)
                if aid:
                    return aid
            if self._account_type == "real" and atype in ("real", "live"):
                aid = _acct_id(acct)
                if aid:
                    return aid

        # Fall back to the first account with a usable id.
        for acct in accounts:
            aid = _acct_id(acct)
            if aid:
                logger.warning(
                    "Deriv: no account matched type '{}', using first available ({})",
                    self._account_type, aid,
                )
                return aid

        logger.error("Deriv accounts response had no usable accountId: {}", accounts)
        return ""

    async def _get_otp_ws_url(self, account_id: str) -> str:
        """Fetch a fresh, single-use OTP WebSocket URL for *account_id*."""
        url = _DERIV_OTP_URL.format(account_id=account_id)
        body = await self._rest_request("POST", url)
        if body is None:
            logger.error("Deriv OTP request failed for account {}", account_id)
            return ""
        # The OTP endpoint returns a ready-to-use, pre-authenticated WS URL.
        ws_url = (
            body.get("ws_url")
            or body.get("url")
            or body.get("websocket_url")
            or body.get("websocketUrl")
        )
        if not ws_url:
            data = body.get("data")
            if isinstance(data, dict):
                ws_url = (
                    data.get("ws_url")
                    or data.get("url")
                    or data.get("websocket_url")
                    or data.get("websocketUrl")
                )
        if not ws_url or not isinstance(ws_url, str):
            logger.error("Deriv OTP response missing WebSocket URL: {}", body)
            return ""
        return ws_url

    @staticmethod
    async def _make_lock() -> asyncio.Lock:
        """Create an asyncio.Lock bound to the running loop (self._loop)."""
        return asyncio.Lock()

    async def _discover_multipliers(self) -> None:
        """Query Deriv contracts_for API to discover valid multipliers per symbol."""
        try:
            cfg_path = __file__.replace(
                "platforms/deriv/deriv_connector.py",
                "config/brokers/deriv.json",
            )
            with open(cfg_path) as f:
                cfg = json.load(f)
            mult_keys = set(cfg.get("multipliers", {}).keys())
            override_vals = set(cfg.get("overrides", {}).values())
            symbols = sorted((mult_keys | override_vals) - {"_default"})
        except Exception:
            symbols = []

        for sym in symbols:
            if sym.startswith("_"):
                continue
            try:
                resp = await self._send_raw(
                    {"contracts_for": sym, "currency": "USD", "product_type": "basic"},
                    allow_reconnect=False,
                )
                if resp.get("error"):
                    continue
                available = resp.get("contracts_for", {}).get("available", [])
                mults: set[int] = set()
                for contract in available:
                    ctype = contract.get("contract_type", "")
                    if ctype not in ("MULTUP", "MULTDOWN"):
                        continue
                    if "multiplier_range" in contract:
                        mrange = contract["multiplier_range"]
                        for v in mrange:
                            mults.add(int(v))
                    if "multipliers" in contract:
                        for v in contract["multipliers"]:
                            mults.add(int(v))
                if mults:
                    sorted_mults = sorted(mults)
                    self._discovered_multipliers[sym] = sorted_mults
                    logger.info("Deriv multipliers discovered — {}: {}", sym, sorted_mults)
            except Exception as exc:
                logger.debug("Multiplier discovery failed for {}: {}", sym, exc)

    def disconnect(self) -> None:
        self._stop_token_monitor()
        if self._ws is not None:
            try:
                future = asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
                future.result(timeout=10)
            except Exception as exc:
                logger.warning("Deriv disconnect error: {}", exc)
        self._connected = False
        self._reconnecting = False
        self._authorized = False
        self._ws = None
        logger.info("Deriv disconnected")

    def is_connected(self) -> bool:
        if self._ws is None or not self._connected:
            return False
        # websockets >= 10 uses .state; older versions expose .open
        state = getattr(self._ws, "state", None)
        if state is not None:
            import websockets.connection as _wsc
            return state == _wsc.State.OPEN
        return bool(getattr(self._ws, "open", False))

    async def _reconnect(self) -> bool:
        self._reconnecting = True
        self._connected = False
        self._authorized = False
        # Close any stale socket so we don't leak connections (Deriv allows
        # only 5 concurrent connections per user).
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        try:
            for attempt in range(1, _MAX_RECONNECT_ATTEMPTS + 1):
                logger.warning("Deriv reconnect attempt {}/{}", attempt, _MAX_RECONNECT_ATTEMPTS)
                # _connect_async fetches a FRESH single-use OTP every call, so
                # each reconnect attempt gets a new pre-authenticated WS URL.
                ok = await self._connect_async()
                if ok:
                    return True
                # If the access token has expired, further attempts are futile
                # until DERIV_ACCESS_TOKEN is refreshed — stop early.
                if self._token_expires_at - _time.time() <= 0:
                    logger.critical(
                        "Deriv reconnect aborted — access token expired. "
                        "Refresh DERIV_ACCESS_TOKEN to restore the connection."
                    )
                    return False
                await asyncio.sleep(_RECONNECT_DELAY * attempt)
            logger.error("Deriv reconnect failed after {} attempts", _MAX_RECONNECT_ATTEMPTS)
            return False
        finally:
            self._reconnecting = False

    # ── Low-level send/receive ───────────────────────────────────────────

    async def _send(self, payload: dict) -> dict:
        async with self._lock:
            return await self._send_raw(payload)

    async def _send_raw(self, payload: dict, allow_reconnect: bool = True) -> dict:
        """Inner send/recv WITHOUT acquiring ``self._lock``.

        Split out from ``_send`` so the reconnect path
        (``_reconnect`` → ``_connect_async`` → ``_discover_multipliers``) can
        re-enter the sender without re-acquiring the non-reentrant lock that the
        public ``_send`` already holds. ``allow_reconnect=False`` is used for
        sends issued from inside a reconnect to avoid recursive reconnects.
        """
        self._req_id += 1
        req_id = self._req_id
        payload["req_id"] = req_id
        for attempt in range(2):  # one retry after reconnect
            try:
                await self._ws.send(json.dumps(payload))
                # Correlate the response to THIS request by req_id. Deriv
                # multiplexes subscription/stream frames (e.g. the buy
                # ``subscribe`` stream, tick updates) onto the same socket,
                # so a blind ``recv()`` can read a stale or out-of-band frame
                # — e.g. a previous failed buy's error — and mis-attribute it
                # to this request. That is what made a documented-correct
                # ``proposal`` (with ``symbol``) appear to fail with the
                # buy's 'Properties not allowed: symbol'. Discard uncorrelated
                # frames until the matching req_id arrives, bounded by the
                # request timeout so a missing reply still fails fast.
                deadline = _time.monotonic() + _REQUEST_TIMEOUT
                while True:
                    remaining = deadline - _time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError(
                            f"No Deriv response for req_id={req_id}"
                        )
                    raw = await asyncio.wait_for(
                        self._ws.recv(), timeout=remaining
                    )
                    msg = json.loads(raw)
                    if msg.get("req_id") == req_id:
                        return msg
                    logger.debug(
                        "Deriv discarding uncorrelated frame req_id={} "
                        "(awaiting {})",
                        msg.get("req_id"), req_id,
                    )
            except Exception as exc:
                if attempt == 0 and allow_reconnect:
                    logger.warning("Deriv send error ({}), reconnecting…", exc)
                    ok = await self._reconnect()
                    if not ok:
                        raise ConnectionError("Deriv reconnect failed") from exc
                    # A lost confirmation on a buy may mean the order actually
                    # landed before the socket dropped. Re-check idempotency
                    # before re-sending to avoid a duplicate contract.
                    dup = await self._check_buy_idempotency(payload, req_id)
                    if dup is not None:
                        return dup
                else:
                    raise

    async def _check_buy_idempotency(
        self, payload: dict, req_id: int,
    ) -> Optional[dict]:
        """Return a synthetic buy response if this buy's idem_key already filled.

        Used after a reconnect inside ``_send_raw`` to prevent re-sending a buy
        whose first attempt may have succeeded. Uses an unlocked portfolio query
        (``_send_raw`` with ``allow_reconnect=False``) so it is safe to call
        while the public ``_send`` lock is held.
        """
        if not payload.get("buy"):
            return None
        idem = (payload.get("passthrough") or {}).get("idem_key")
        if not idem:
            return None
        for cid, info in self._positions.items():
            if info.get("idem_key") == idem:
                logger.warning(
                    "Deriv reconnect duplicate prevented — idem_key {} "
                    "already tracked as contract {}", idem, cid,
                )
                return {"buy": {"contract_id": cid}, "req_id": req_id}
        try:
            pf = await self._send_raw(
                {"portfolio": 1, "contract_type": ["MULTUP", "MULTDOWN"]},
                allow_reconnect=False,
            )
            for c in pf.get("portfolio", {}).get("contracts", []):
                pt = c.get("passthrough") or {}
                if pt.get("idem_key") == idem:
                    cid = str(c.get("contract_id", ""))
                    logger.warning(
                        "Deriv reconnect duplicate prevented — idem_key {} "
                        "already filled as contract {}", idem, cid,
                    )
                    return {"buy": {"contract_id": cid}, "req_id": req_id}
        except Exception as exc:
            logger.warning("[deriv] reconnect idempotency lookup failed: {}", exc)
        return None

    def _reserve_history_slot(self) -> float:
        """Reserve the next ``ticks_history`` send slot.

        Returns the number of seconds the caller must wait (sleep) before
        sending so that consecutive history requests stay at least
        ``_HISTORY_MIN_INTERVAL`` apart. Thread-safe: concurrent callers each
        reserve a distinct, monotonically-spaced slot, so the per-second rate
        limit is preserved even under contention. The lock is held only for the
        O(1) reservation arithmetic — the actual wait happens in the caller,
        outside any lock.
        """
        with self._history_lock:
            now = _time.monotonic()
            earliest = self._next_history_at if self._next_history_at > now else now
            self._next_history_at = earliest + _HISTORY_MIN_INTERVAL
            self._last_history_request = earliest
            return earliest - now

    def _sync_send(self, payload: dict) -> dict:
        # Rate-limit history requests by reserving a spaced slot and sleeping
        # BEFORE submitting — never while holding a lock — so price/balance/buy
        # calls are not serialised behind a history wait. The asyncio _send
        # coroutine still serialises actual WS frame send/recv via self._lock.
        if "ticks_history" in payload:
            wait = self._reserve_history_slot()
            if wait > 0:
                _time.sleep(wait)
        future = asyncio.run_coroutine_threadsafe(self._send(payload), self._loop)
        return future.result(timeout=_REQUEST_TIMEOUT + 5)

    # ── Account ──────────────────────────────────────────────────────────

    def get_account_info(self) -> AccountInfo:
        self._require_connection()
        now = _time.monotonic()
        # Serialise balance fetches: a burst of callers collapses to one API
        # hit, the rest get the cached value.
        with self._balance_lock:
            cache = self._acct_cache
            age = now - self._acct_cache_ts
            if cache is not None and age < _BALANCE_CACHE_TTL:
                return cache
            # Deriv does not support subscribe=0 on the ticks endpoint (see
            # get_price comment at L282).  The balance endpoint has the same
            # limitation — omit subscribe to avoid silent rejection.
            resp = self._sync_send({"balance": 1})
            if resp.get("error"):
                err = resp["error"]
                # During a rate-limit storm, serve a recent cached value instead
                # of failing the whole balance-dependent path.
                if cache is not None and age < _BALANCE_MAX_STALE:
                    logger.debug(
                        "Deriv balance error (code={}) — serving cached balance "
                        "{:.0f}s old",
                        err.get("code"), age,
                    )
                    return cache
                raise RuntimeError(
                    f"Deriv balance error: code={err.get('code')}, "
                    f"message={err.get('message')}"
                )
            bal = resp.get("balance", {})
            info = AccountInfo(
                balance=float(bal.get("balance", 0)),
                equity=float(bal.get("balance", 0)),
                margin=0.0,
                free_margin=float(bal.get("balance", 0)),
                margin_level=0.0,
                currency=bal.get("currency", "USD"),
                leverage=1,
                platform="deriv",
            )
            self._acct_cache = info
            self._acct_cache_ts = now
            return info

    # ── Market data ──────────────────────────────────────────────────────

    def get_price(self, symbol: str) -> TickData:
        self._require_connection()
        mapped = self.symbol_map(symbol)
        # Deriv does not support subscribe=0 on the ticks endpoint.
        # Use ticks_history with count=1 for a one-shot latest price instead.
        resp = self._sync_send({
            "ticks_history": mapped,
            "count": 1,
            "end": "latest",
            "style": "ticks",
            "adjust_start_time": 1,
        })
        if resp.get("error"):
            raise RuntimeError(f"Deriv tick error: {resp['error'].get('message')}")
        history = resp.get("history", {})
        prices = history.get("prices", [])
        times  = history.get("times", [])
        if not prices:
            resp_keys = list(resp.keys())
            echo = resp.get("echo_req", {})
            raise RuntimeError(
                f"No tick data from Deriv for {mapped} "
                f"(resp_keys={resp_keys}, echo_req={echo})"
            )
        quote = float(prices[-1])
        epoch = int(times[-1]) if times else 0

        if quote <= 0:
            logger.warning(
                "Non-positive tick for {}: quote={}", mapped, quote
            )
            raise RuntimeError(
                f"Non-positive tick for {mapped}: quote={quote}"
            )

        if epoch <= 0:
            logger.warning(
                "Invalid tick timestamp for {}: epoch={}", mapped, epoch
            )
            raise RuntimeError(
                f"Invalid tick timestamp for {mapped}: epoch={epoch}"
            )
        tick_time = datetime.fromtimestamp(epoch, tz=timezone.utc)
        age = (datetime.now(timezone.utc) - tick_time).total_seconds()
        max_tick_age = getattr(self, "_max_tick_age_seconds", 120.0)
        if age > max_tick_age:
            logger.warning(
                "Stale tick for {}: {:.1f}s old (limit {}s)",
                mapped, age, max_tick_age,
            )
            raise RuntimeError(
                f"Stale tick for {mapped}: {age:.1f}s old "
                f"(limit {max_tick_age}s)"
            )

        # ticks_history returns mid price only — bid/ask not available.
        # Spread is effectively 0 from this endpoint; bootstrap uses it only
        # for instruments where bid/ask are unavailable. For spread purposes
        # the hardcoded registry fallback will be used for synthetics.
        return TickData(
            bid=quote,
            ask=quote,
            spread=0.0,
            time=tick_time,
        )

    def get_tick(self, symbol: str) -> TickData:
        return self.get_price(symbol)

    def get_ohlcv(
        self, symbol: str, timeframe: str, count: int = 200
    ) -> pd.DataFrame:
        self._require_connection()
        mapped = self.symbol_map(symbol)
        granularity = self.timeframe_map(timeframe)
        resp = self._sync_send({
            "ticks_history": mapped,
            "adjust_start_time": 1,
            "count": count,
            "end": "latest",
            "granularity": granularity,
            "style": "candles",
        })
        if resp.get("error"):
            raise RuntimeError(
                f"Deriv candles error for {mapped}: {resp['error'].get('message')}"
            )
        candles = resp.get("candles", [])
        if not candles:
            resp_keys = list(resp.keys())
            echo = resp.get("echo_req", {})
            raise RuntimeError(
                f"No candle data for {mapped}/{timeframe} "
                f"(granularity={granularity}, resp_keys={resp_keys}, "
                f"echo_req={echo})"
            )

        rows = []
        for c in candles:
            rows.append({
                "time": datetime.fromtimestamp(c["epoch"], tz=timezone.utc),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": int(c.get("volume", 0)),
            })
        return pd.DataFrame(rows)

    def get_spread(self, symbol: str) -> float:
        return self.get_price(symbol).spread

    # ── Order execution ──────────────────────────────────────────────────

    def _accepted_multipliers(self, mapped_symbol: str) -> tuple[list[int], int]:
        """Return (sorted accepted multipliers, desired default) for a symbol.

        Prefers runtime-discovered values over the static JSON config, falling
        back to a conservative built-in list when neither is available. The
        fallback is logged loudly (once per symbol) because trading on a guessed
        multiplier set is a degraded mode — discovery or config should supply the
        real values.
        """
        accepted: Optional[list[int]] = None
        source = "fallback"

        if self._discovered_multipliers.get(mapped_symbol):
            accepted = list(self._discovered_multipliers[mapped_symbol])
            source = "discovered"

        desired = 1000
        try:
            cfg_path = Path(__file__).resolve().parent.parent.parent / "config" / "brokers" / "deriv.json"
            with open(cfg_path) as f:
                cfg = json.load(f)
            mult_map = cfg.get("multipliers", {})
            entry = mult_map.get(mapped_symbol) or mult_map.get("_default", {})
            desired = int(entry.get("default", 1000))
            if accepted is None and entry.get("accepted"):
                accepted = [int(x) for x in entry["accepted"]]
                source = "config"
        except Exception:
            desired = 1000

        if not accepted:
            accepted = list(_FALLBACK_ACCEPTED_MULTIPLIERS)
            source = "fallback"

        if source == "fallback":
            self._warn_multiplier_fallback(mapped_symbol)

        return sorted(set(int(x) for x in accepted)), desired

    def _warn_multiplier_fallback(self, mapped_symbol: str) -> None:
        """Warn (once per symbol) that we are guessing the multiplier set.

        Surfaces a silent discovery/config gap as a WARNING so it is visible in
        the live logs instead of only at DEBUG. The order will still attempt the
        conservative fallback and self-correct from Deriv's error.details if the
        guessed values are rejected.
        """
        warned = getattr(self, "_warned_fallback_symbols", None)
        if warned is None:
            warned = set()
            self._warned_fallback_symbols = warned
        if mapped_symbol in warned:
            return
        warned.add(mapped_symbol)
        logger.warning(
            "Deriv multipliers for {} not discovered and absent from "
            "config/brokers/deriv.json — using conservative fallback {}. "
            "Order will self-correct from Deriv error.details if rejected; "
            "populate deriv.json or verify discovery ran.",
            mapped_symbol, _FALLBACK_ACCEPTED_MULTIPLIERS,
        )

    @staticmethod
    def _error_text(error_obj: object) -> str:
        """Flatten a Deriv error object (message + details) into one string.

        Deriv returns the generic 'Input validation failed: parameters' in
        ``message`` and the field-specific reason — including the accepted
        multiplier list — in ``details``. Concatenating both lets the multiplier
        and stake-cap regexes match the real cause instead of the generic
        envelope, so the retry loop can self-correct.
        """
        if not isinstance(error_obj, dict):
            return str(error_obj) if error_obj else "Unknown error"
        parts: list[str] = []
        msg = error_obj.get("message")
        if msg:
            parts.append(str(msg))
        details = error_obj.get("details")
        if isinstance(details, dict):
            for value in details.values():
                if value:
                    parts.append(str(value))
        elif details:
            parts.append(str(details))
        return " | ".join(parts) if parts else "Unknown error"

    @staticmethod
    def _is_symbol_property_error(err: str) -> bool:
        """True when Deriv rejected ``symbol`` inside the buy ``parameters``.

        Deriv's buy-by-parameters schema rejects ``symbol`` on this endpoint with
        'Input validation failed: parameters | Properties not allowed: symbol'.
        The buy-by-parameters form cannot satisfy it; the order must instead go
        through the proposal→buy flow where ``symbol`` lives in the proposal
        (contract definition) and the buy references the returned proposal id.
        """
        if not err:
            return False
        e = err.lower()
        return "symbol" in e and (
            "properties not allowed" in e
            or "property not allowed" in e
            or "additional propert" in e
        )

    def _get_multiplier(self, mapped_symbol: str) -> int:
        """Look up the correct multiplier for a Deriv symbol.
        Prefers runtime-discovered values over static JSON config.
        Snaps the default value to the nearest accepted multiplier so Deriv
        never rejects the order with 'Multiplier is not in acceptable range'.
        """
        accepted, desired = self._accepted_multipliers(mapped_symbol)
        nearest = min(accepted, key=lambda x: abs(x - desired))
        return nearest

    @staticmethod
    def _fit_multiplier_for_sl(
        multiplier: int,
        sl_pct: float,
        accepted: list[int],
        safety: float = _SL_STAKE_SAFETY,
    ) -> int:
        """Snap the multiplier DOWN so the SL is reachable before liquidation.

        With Deriv multipliers the maximum possible loss equals the stake, so the
        ``limit_order.stop_loss`` (in account currency) is
        ``stake × multiplier × sl_pct``. If that exceeds the stake the contract
        would liquidate before the technical SL and Deriv rejects the order with
        'Input validation failed: parameters'. Keeping ``multiplier × sl_pct``
        below ``safety`` (a small commission buffer under 1.0) guarantees the
        stop-loss dollar value stays within the stake.

        Returns the largest accepted multiplier that satisfies the bound, or the
        smallest accepted multiplier when even that is too high (the caller then
        clamps the stop-loss dollar value as a last resort).
        """
        if sl_pct <= 0 or not accepted:
            return multiplier
        max_mult = safety / sl_pct
        fitting = [m for m in accepted if m <= max_mult and m <= multiplier]
        if fitting:
            return max(fitting)
        # Nothing within the original multiplier fits — fall back to the
        # smallest accepted multiplier to minimise the overshoot.
        return min(accepted)

    @staticmethod
    def _limit_order_dollars(
        sl_pct: float,
        tp_pct: float,
        amount: float,
        multiplier: int,
        safety: float = _SL_STAKE_SAFETY,
    ) -> tuple[float, float]:
        """Compute Deriv ``limit_order`` stop_loss / take_profit dollar values.

        The stop-loss is clamped to ``amount × safety`` so it can never exceed
        the stake (the broker's hard cap), which is the validation Deriv enforces
        on multiplier contracts. Both values are rounded to cents and floored at
        a small positive minimum so the payload is always accepted.
        """
        _MIN_LIMIT = 0.01
        stop_loss = sl_pct * amount * multiplier
        take_profit = tp_pct * amount * multiplier
        max_stop = max(_MIN_LIMIT, round(amount * safety, 2))
        stop_loss = min(round(max(_MIN_LIMIT, stop_loss), 2), max_stop)
        take_profit = round(max(_MIN_LIMIT, take_profit), 2)
        return stop_loss, take_profit

    def place_order(
        self,
        symbol: str,
        direction: str,
        lots: float,
        sl: float,
        tp: float,
        comment: str = "",
        idempotency_key: str = "",
        # ── Deriv-specific kwargs ──────────────────────────────────────────
        # Pass stake_usd to bypass the lots→stake conversion entirely.
        # PlatformManager sets this when routing a Deriv synthetic order.
        stake_usd: Optional[float] = None,
        multiplier: Optional[int] = None,
    ) -> OrderResult:
        self._require_connection()
        mapped = self.symbol_map(symbol)
        is_buy = direction.upper() in ("BUY", "LONG")

        # Resolve multiplier from broker config if not explicitly passed
        if multiplier is None:
            multiplier = self._get_multiplier(mapped)

        # Use get_price (ticks_history) — avoids subscribe:0 validation error
        _tick = self.get_price(symbol)
        price = _tick.ask if is_buy else _tick.bid

        contract_type = "MULTUP" if is_buy else "MULTDOWN"

        # ── Fit the multiplier to the SL distance ───────────────────────────
        # A Deriv multiplier contract liquidates at a loss equal to the stake,
        # so the protective stop can only be honoured when
        # multiplier × (sl_distance / price) stays below 1.0. A too-high
        # multiplier (e.g. the 1000× default against a wide SL) makes the
        # stop_loss dollar value exceed the stake and Deriv rejects the order.
        # Snap the multiplier DOWN to the largest accepted value that keeps the
        # SL reachable before liquidation.
        sl_pct_initial = abs(price - sl) / price if price > 0 else 0.0
        if sl_pct_initial > 0:
            accepted_mults, _ = self._accepted_multipliers(mapped)
            fitted = self._fit_multiplier_for_sl(multiplier, sl_pct_initial, accepted_mults)
            if fitted != multiplier:
                logger.warning(
                    "Deriv multiplier {}× too high for {} SL ({:.3f}% away) — "
                    "fitting down to {}× so the stop stays within the stake.",
                    multiplier, mapped, sl_pct_initial * 100, fitted,
                )
                multiplier = fitted

        # ── Stake calculation ──────────────────────────────────────────────
        # Deriv Multipliers work on a USD stake, NOT on lots.
        # Formula: stake = risk_amount / (sl_distance_pct * multiplier)
        # where sl_distance_pct = |price - sl| / price
        #
        # If stake_usd is supplied directly (from RiskEngine), use it.
        # Otherwise derive it from the SL distance so the risk stays correct.
        if stake_usd is not None:
            amount = round(max(1.0, stake_usd), 2)
        else:
            sl_distance = abs(price - sl)
            if sl_distance > 0 and price > 0:
                # Risk% of account that the SL represents at this multiplier:
                # P&L = stake × multiplier × (Δprice / price)
                # Max loss = stake × multiplier × (sl_distance / price)
                # → stake = max_loss / (multiplier × sl_distance / price)
                # lots here carries the risk_amount already encoded by the sizer,
                # so we back-calculate: risk_amount = lots * risk_pips * pip_value
                # For synthetics pip_value = 1.0 (from registry)
                pip = get_pip_size(symbol)
                risk_pips = sl_distance / pip if pip else sl_distance
                risk_amount = lots * risk_pips * 1.0   # pip_value_per_lot = 1 for synthetics
                stake = risk_amount / (multiplier * sl_distance / price)
                amount = round(max(1.0, stake), 2)
            else:
                # Fallback: 1% of account proxy — will be overridden by stake_usd path
                amount = round(max(1.0, lots * 10), 2)

        logger.debug(
            "Deriv stake — {} {} | price={} SL={} | stake=${} multiplier={}×",
            direction, symbol, price, sl, amount, multiplier,
        )

        if idempotency_key:
            dup_cid = self._find_contract_by_idem_key(idempotency_key)
            if dup_cid:
                logger.warning(
                    "Deriv duplicate prevented — idem_key {} already filled as contract {}",
                    idempotency_key, dup_cid,
                )
                return OrderResult(
                    success=True,
                    order_id=dup_cid,
                    fill_price=price,
                    requested_price=price,
                    slippage_pips=0.0,
                    lots=lots,
                    symbol=symbol,
                    direction=direction.upper(),
                    sl=sl,
                    tp=tp,
                    platform="deriv",
                )

        passthrough: dict[str, str] = {}
        if idempotency_key:
            passthrough["idem_key"] = idempotency_key

        # SL/TP distances as a fraction of price — reused by the retry path when
        # it recomputes the limit_order dollar values after an amount/multiplier
        # change (see _limit_order_dollars below).
        sl_pct = abs(price - sl) / price if price > 0 else 0.0
        tp_pct = abs(tp - price) / price if price > 0 else 0.0

        send_limit_order = True
        initial_sl_dollar = round(sl_pct * amount * multiplier, 2)
        initial_tp_dollar = round(tp_pct * amount * multiplier, 2)

        def build_order_payload(amount: float, multiplier: int, limit_order_enabled: bool) -> dict:
            payload = {
                "buy": 1,
                "price": amount,
                "parameters": {
                    "contract_type": contract_type,
                    "symbol": mapped,
                    "currency": "USD",
                    "amount": amount,
                    "basis": "stake",
                    "multiplier": multiplier,
                },
            }
            if limit_order_enabled:
                payload["parameters"]["limit_order"] = {
                    "stop_loss": initial_sl_dollar,
                    "take_profit": initial_tp_dollar,
                }
            if passthrough:
                payload["passthrough"] = passthrough
            return payload

        buy_payload = build_order_payload(amount, multiplier, send_limit_order)
        t0 = _time.monotonic()
        resp = self._sync_send(buy_payload)
        latency = (_time.monotonic() - t0) * 1000

        # ── Auto-retry loop ────────────────────────────────────────────────
        # Deriv can reject for wrong multiplier or stake cap.
        # SL/TP dollar values MUST be recomputed after every amount change so
        # Deriv's cap (which scales with the SL dollar magnitude) converges.
        import re as _re
        import math as _math

        MAX_RETRIES = 5
        _MIN_STAKE = 1.0
        err: Optional[str] = self._error_text(resp["error"]) if resp.get("error") else None
        use_proposal_fallback = False

        for _attempt in range(MAX_RETRIES):
            if err is None:
                break

            # ── 0. Symbol property rejection ───────────────────────────────
            # Deriv rejects ``symbol`` inside the buy ``parameters`` on this
            # endpoint ('Properties not allowed: symbol'). The buy-by-parameters
            # form cannot satisfy it, so break out and recover via the canonical
            # proposal→buy flow below. Detecting it first stops the generic
            # limit_order strip from misattributing the cause to SL/TP.
            if self._is_symbol_property_error(err):
                use_proposal_fallback = True
                break

            changed = False

            # ── 1. Multiplier correction (highest priority — the real cause) ──
            # Deriv puts the accepted list in error.details; _error_text already
            # folded that into ``err`` so this regex can fire. Fixing the
            # multiplier first prevents misattributing the rejection to SL/TP.
            _mult_match = _re.search(
                r"Multiplier is not in acceptable range.*?Accepts\s+([\d,\s]+)", err
            )
            if _mult_match:
                valid = sorted(int(x.strip()) for x in _mult_match.group(1).split(",") if x.strip().isdigit())
                if valid:
                    self._discovered_multipliers[mapped] = valid
                    nearest = min(valid, key=lambda x: abs(x - multiplier))
                    # Re-fit against the now-known valid list so the SL still
                    # fits within the stake (uses the existing fitting math).
                    corrected = self._fit_multiplier_for_sl(nearest, sl_pct, valid)
                    logger.warning(
                        "Deriv multiplier {} rejected for {} — retrying with {} (valid: {}). "
                        "Update config/brokers/deriv.json!",
                        multiplier, mapped, corrected, valid,
                    )
                    if corrected > 0:
                        amount = round(max(_MIN_STAKE, amount * multiplier / corrected), 2)
                    multiplier = corrected
                    changed = True

            # ── 2. Stake cap ───────────────────────────────────────────────
            _cap_match = _re.search(r"equal to or lower than ([\d]+(?:\.[\d]+)?)", err)
            if _cap_match:
                max_stake = float(_cap_match.group(1))
                capped = max(_MIN_STAKE, float(_math.floor(max_stake * 100 - 1)) / 100)
                if capped >= amount:
                    capped = max(_MIN_STAKE, amount - 0.50)
                if capped < _MIN_STAKE:
                    logger.warning(
                        "Deriv cap ${:.2f} below minimum ${:.2f} — skipping {} {}",
                        max_stake, _MIN_STAKE, direction, symbol,
                    )
                    break
                if capped < amount:
                    logger.warning(
                        "Deriv stake capped — {} {} floored to ${:.2f} (broker cap ${:.2f})",
                        direction, symbol, capped, max_stake,
                    )
                    amount = capped
                    changed = True

            # ── 3. limit_order strip — only when SL/TP is the real culprit ──
            # Drop the SL/TP bounds when the error explicitly references them, or
            # as a last resort for a generic validation failure that nothing else
            # has already addressed. Reordering this AFTER multiplier/cap stops a
            # generic 'Input validation failed' from blindly blaming SL/TP while
            # the actual cause (a bad multiplier) goes uncorrected.
            _err_low = err.lower()
            references_sltp = any(
                k in _err_low
                for k in ("stop_loss", "take_profit", "stop loss", "take profit",
                          "limit_order", "limit order")
            )
            if send_limit_order and (
                references_sltp
                or (not changed and "Input validation failed: parameters" in err)
            ):
                logger.warning(
                    "Deriv rejected limit_order (SL/TP) params for {} — retrying without limit_order. "
                    "Check SL/TP bounds in config.",
                    mapped,
                )
                send_limit_order = False
                changed = True

            if not changed:
                break

            # Recompute SL/TP dollar values from current amount so the cap
            # does not slide due to a stale, oversized stop_loss value. The
            # stop_loss is clamped to stay within the (possibly reduced) stake.
            sl_dollar, tp_dollar = self._limit_order_dollars(
                sl_pct, tp_pct, amount, multiplier,
            )

            retry_payload = build_order_payload(amount, multiplier, send_limit_order)
            if send_limit_order:
                retry_payload["parameters"]["limit_order"] = {
                    "stop_loss": sl_dollar,
                    "take_profit": tp_dollar,
                }

            resp = self._sync_send(retry_payload)
            err = self._error_text(resp["error"]) if resp.get("error") else None

            if self._is_symbol_property_error(err):
                use_proposal_fallback = True
                break

        if use_proposal_fallback:
            # Deriv rejected ``symbol`` in the buy ``parameters``. Recover via the
            # canonical proposal→buy flow. Pass the (already multiplier-fitted)
            # amount/multiplier so the proposal starts from the corrected values.
            return self._buy_via_proposal(
                mapped=mapped,
                contract_type=contract_type,
                amount=amount,
                multiplier=multiplier,
                sl_pct=sl_pct,
                tp_pct=tp_pct,
                passthrough=passthrough,
                entry_price=price,
                lots=lots,
                symbol=symbol,
                direction=direction,
                sl=sl,
                tp=tp,
                idempotency_key=idempotency_key,
            )

        if err:
            logger.error("Deriv order failed — {} {} {}: {}", direction, mapped, lots, err)
            return OrderResult(
                success=False, order_id="", fill_price=0.0,
                requested_price=price, slippage_pips=0.0, lots=lots,
                symbol=symbol, direction=direction.upper(), sl=sl, tp=tp,
                platform="deriv", error=err,
            )

        buy_resp = resp.get("buy", {})
        contract_id = str(buy_resp.get("contract_id", "")).strip()

        # Guard: Deriv sometimes returns a buy response without a contract_id
        # (e.g. partial fills, balance-check responses, network hiccups).
        # Without a real contract_id we cannot track, modify, or close the position.
        # Treat this as a failure so no ghost trade is recorded in the dashboard.
        if not contract_id or contract_id == "0":
            logger.error(
                "Deriv order response missing contract_id — treating as FAILED. "
                "buy_resp={}", buy_resp,
            )
            return OrderResult(
                success=False, order_id="", fill_price=0.0,
                requested_price=price, slippage_pips=0.0, lots=lots,
                symbol=symbol, direction=direction.upper(), sl=sl, tp=tp,
                platform="deriv",
                error="No contract_id in Deriv response — order not confirmed",
            )

        self._positions[contract_id] = {
            "symbol": symbol, "direction": direction.upper(),
            "lots": lots, "sl": sl, "tp": tp, "open_price": price,
            "stake": amount, "multiplier": multiplier,
            "idem_key": idempotency_key,
        }
        self._persist_position(contract_id)

        logger.info(
            "Deriv order filled — {} {} {} lots @ {} contract={} ({:.0f}ms)",
            direction, mapped, lots, amount, contract_id, latency,
        )
        return OrderResult(
            success=True,
            order_id=contract_id,
            fill_price=price,
            requested_price=price,
            slippage_pips=0.0,
            lots=lots,
            symbol=symbol,
            direction=direction.upper(),
            sl=sl,
            tp=tp,
            platform="deriv",
        )

    def _buy_via_proposal(
        self,
        *,
        mapped: str,
        contract_type: str,
        amount: float,
        multiplier: int,
        sl_pct: float,
        tp_pct: float,
        passthrough: dict,
        entry_price: float,
        lots: float,
        symbol: str,
        direction: str,
        sl: float,
        tp: float,
        idempotency_key: str = "",
    ) -> OrderResult:
        """Buy a multiplier contract via the proposal→buy flow.

        Used when Deriv rejects ``symbol`` inside the buy ``parameters``
        ('Properties not allowed: symbol'). Here ``symbol`` lives in the
        ``proposal`` request (the contract definition) — where Deriv accepts
        it — and the ``buy`` references the returned proposal id, so no
        ``symbol`` property is ever sent on the buy request itself.

        The proposal request carries the same multiplier-fit / stake-cap /
        limit_order self-correction the buy-by-parameters path uses, so a
        rejected multiplier or oversized stop still converges.
        """
        import re as _re
        import math as _math

        MAX_RETRIES = 5
        _MIN_STAKE = 1.0
        send_limit_order = True

        def build_proposal_payload(amount: float, multiplier: int, limit_order_enabled: bool) -> dict:
            params: dict[str, Any] = {
                "proposal": 1,
                "amount": amount,
                "basis": "stake",
                "contract_type": contract_type,
                "currency": "USD",
                "symbol": mapped,
                "multiplier": multiplier,
            }
            if limit_order_enabled:
                sl_dollar, tp_dollar = self._limit_order_dollars(
                    sl_pct, tp_pct, amount, multiplier,
                )
                params["limit_order"] = {
                    "stop_loss": sl_dollar,
                    "take_profit": tp_dollar,
                }
            return params

        t0 = _time.monotonic()
        resp = self._sync_send(build_proposal_payload(amount, multiplier, send_limit_order))
        err: Optional[str] = self._error_text(resp["error"]) if resp.get("error") else None

        for _attempt in range(MAX_RETRIES):
            if err is None:
                break

            changed = False

            # ── 1. Multiplier correction ───────────────────────────────────
            _mult_match = _re.search(
                r"Multiplier is not in acceptable range.*?Accepts\s+([\d,\s]+)", err
            )
            if _mult_match:
                valid = sorted(int(x.strip()) for x in _mult_match.group(1).split(",") if x.strip().isdigit())
                if valid:
                    self._discovered_multipliers[mapped] = valid
                    nearest = min(valid, key=lambda x: abs(x - multiplier))
                    corrected = self._fit_multiplier_for_sl(nearest, sl_pct, valid)
                    logger.warning(
                        "Deriv multiplier {} rejected for {} (proposal) — retrying with {} (valid: {}).",
                        multiplier, mapped, corrected, valid,
                    )
                    if corrected > 0:
                        amount = round(max(_MIN_STAKE, amount * multiplier / corrected), 2)
                    multiplier = corrected
                    changed = True

            # ── 2. Stake cap ───────────────────────────────────────────────
            _cap_match = _re.search(r"equal to or lower than ([\d]+(?:\.[\d]+)?)", err)
            if _cap_match:
                max_stake = float(_cap_match.group(1))
                capped = max(_MIN_STAKE, float(_math.floor(max_stake * 100 - 1)) / 100)
                if capped >= amount:
                    capped = max(_MIN_STAKE, amount - 0.50)
                if capped < _MIN_STAKE:
                    logger.warning(
                        "Deriv cap ${:.2f} below minimum ${:.2f} (proposal) — skipping {} {}",
                        max_stake, _MIN_STAKE, direction, symbol,
                    )
                    break
                if capped < amount:
                    amount = capped
                    changed = True

            # ── 3. limit_order strip — only when SL/TP is the real culprit ──
            _err_low = err.lower()
            references_sltp = any(
                k in _err_low
                for k in ("stop_loss", "take_profit", "stop loss", "take profit",
                          "limit_order", "limit order")
            )
            if send_limit_order and (
                references_sltp
                or (not changed and "Input validation failed: parameters" in err)
            ):
                logger.warning(
                    "Deriv rejected limit_order (SL/TP) params for {} (proposal) — retrying without limit_order.",
                    mapped,
                )
                send_limit_order = False
                changed = True

            if not changed:
                break

            resp = self._sync_send(build_proposal_payload(amount, multiplier, send_limit_order))
            err = self._error_text(resp["error"]) if resp.get("error") else None

        if err:
            logger.error(
                "Deriv proposal failed — {} {} {}: {}", direction, mapped, lots, err,
            )
            return OrderResult(
                success=False, order_id="", fill_price=0.0,
                requested_price=entry_price, slippage_pips=0.0, lots=lots,
                symbol=symbol, direction=direction.upper(), sl=sl, tp=tp,
                platform="deriv", error=err,
            )

        proposal = resp.get("proposal", {})
        proposal_id = str(proposal.get("id", "")).strip()
        try:
            ask_price = float(proposal.get("ask_price", amount) or amount)
        except (TypeError, ValueError):
            ask_price = amount
        if not proposal_id:
            return OrderResult(
                success=False, order_id="", fill_price=0.0,
                requested_price=entry_price, slippage_pips=0.0, lots=lots,
                symbol=symbol, direction=direction.upper(), sl=sl, tp=tp,
                platform="deriv",
                error="No proposal id in Deriv response — order not confirmed",
            )

        # Buy by proposal id — no ``symbol``/``parameters`` on the buy request.
        buy_payload: dict[str, Any] = {"buy": proposal_id, "price": ask_price}
        if passthrough:
            buy_payload["passthrough"] = passthrough

        resp = self._sync_send(buy_payload)
        latency = (_time.monotonic() - t0) * 1000
        if resp.get("error"):
            err = self._error_text(resp["error"])
            logger.error(
                "Deriv buy (proposal id) failed — {} {} {}: {}",
                direction, mapped, lots, err,
            )
            return OrderResult(
                success=False, order_id="", fill_price=0.0,
                requested_price=entry_price, slippage_pips=0.0, lots=lots,
                symbol=symbol, direction=direction.upper(), sl=sl, tp=tp,
                platform="deriv", error=err,
            )

        buy_resp = resp.get("buy", {})
        contract_id = str(buy_resp.get("contract_id", "")).strip()
        if not contract_id or contract_id == "0":
            logger.error(
                "Deriv proposal buy response missing contract_id — treating as "
                "FAILED. buy_resp={}", buy_resp,
            )
            return OrderResult(
                success=False, order_id="", fill_price=0.0,
                requested_price=entry_price, slippage_pips=0.0, lots=lots,
                symbol=symbol, direction=direction.upper(), sl=sl, tp=tp,
                platform="deriv",
                error="No contract_id in Deriv response — order not confirmed",
            )

        self._positions[contract_id] = {
            "symbol": symbol, "direction": direction.upper(),
            "lots": lots, "sl": sl, "tp": tp, "open_price": entry_price,
            "stake": amount, "multiplier": multiplier,
            "idem_key": idempotency_key,
        }
        self._persist_position(contract_id)

        logger.info(
            "Deriv order filled (proposal) — {} {} {} lots @ {} contract={} ({:.0f}ms)",
            direction, mapped, lots, amount, contract_id, latency,
        )
        return OrderResult(
            success=True,
            order_id=contract_id,
            fill_price=entry_price,
            requested_price=entry_price,
            slippage_pips=0.0,
            lots=lots,
            symbol=symbol,
            direction=direction.upper(),
            sl=sl,
            tp=tp,
            platform="deriv",
        )

    def modify_order(
        self,
        order_id: str,
        new_sl: Optional[float] = None,
        new_tp: Optional[float] = None,
    ) -> bool:
        self._require_connection()
        limit_order: dict[str, Any] = {}
        pos = self._positions.get(order_id, {})
        open_price = pos.get("open_price", 0)
        stake = pos.get("stake", 0)
        multiplier = pos.get("multiplier", 0)

        if open_price > 0 and stake > 0 and multiplier > 0:
            # Clamp SL to the stake (Deriv hard cap) the same way place_order
            # does. Without this, a breakeven/trailing move can produce a
            # stop_loss dollar value above the stake and Deriv silently rejects
            # the contract_update. Keep the exact contract formula otherwise.
            if new_sl is not None:
                sl_dollar = round(abs(open_price - new_sl) / open_price * stake * multiplier, 2)
                limit_order["stop_loss"] = min(sl_dollar, round(stake, 2))
            if new_tp is not None:
                limit_order["take_profit"] = round(abs(new_tp - open_price) / open_price * stake * multiplier, 2)
        else:
            logger.error("Deriv modify_order missing position data for {} — open_price={} stake={} mult={}", order_id, open_price, stake, multiplier)
            return False

        if not limit_order:
            return True

        resp = self._sync_send({
            "contract_update": 1,
            "contract_id": int(order_id),
            "limit_order": limit_order,
        })
        if resp.get("error"):
            logger.error("Deriv modify failed for {}: {}", order_id, resp["error"].get("message"))
            return False

        if new_sl is not None and order_id in self._positions:
            self._positions[order_id]["sl"] = new_sl
        if new_tp is not None and order_id in self._positions:
            self._positions[order_id]["tp"] = new_tp
        self._persist_position(order_id)
        logger.info("Deriv modified {} — SL={} TP={}", order_id, new_sl, new_tp)
        return True

    def close_order(
        self, order_id: str, lots: Optional[float] = None
    ) -> CloseResult:
        self._require_connection()
        resp = self._sync_send({"sell": int(order_id), "price": 0})
        if resp.get("error"):
            err = resp["error"].get("message", "Unknown")
            logger.error("Deriv close failed for {}: {}", order_id, err)
            return CloseResult(
                success=False, order_id=order_id, close_price=0.0,
                lots_closed=0.0, pnl=0.0, platform="deriv", error=err,
            )

        sell_resp = resp.get("sell", {})
        pnl = float(sell_resp.get("sold_for", 0)) - float(sell_resp.get("buy_price", 0))
        pos = self._positions.pop(order_id, {})
        self._unpersist_position(order_id)

        logger.info("Deriv closed {} — PnL {:.2f}", order_id, pnl)
        return CloseResult(
            success=True,
            order_id=order_id,
            close_price=float(sell_resp.get("sold_for", 0)),
            lots_closed=pos.get("lots", lots or 0),
            pnl=pnl,
            platform="deriv",
        )

    def get_deal_close_info(self, order_id: str) -> Optional[DealCloseInfo]:
        try:
            self._require_connection()
            resp = self._sync_send({
                "proposal_open_contract": 1,
                "contract_id": int(order_id),
            })
            if resp.get("error"):
                return None
            poc = resp.get("proposal_open_contract", {})

            is_sold = poc.get("is_sold") == 1
            settled_status = poc.get("status") in ("sold", "won", "lost")
            if not (is_sold or settled_status):
                return None

            pnl = float(poc.get("profit", 0) or 0)
            close_price = float(poc.get("sell_price", 0) or 0) or None
            close_time = (
                datetime.fromtimestamp(poc["sell_time"], tz=timezone.utc)
                if poc.get("sell_time")
                else None
            )

            local = self._positions.get(order_id, {})
            local_sl = local.get("sl", 0) or 0
            local_tp = local.get("tp", 0) or 0

            exit_reason = "MANUAL"
            if close_price is not None:
                tol = max(abs(close_price) * 1e-4, 1e-9)
                if local_sl and abs(close_price - local_sl) <= tol:
                    exit_reason = "SL"
                elif local_tp and abs(close_price - local_tp) <= tol:
                    exit_reason = "TP"
                elif poc.get("status") == "lost":
                    exit_reason = "STOP_OUT"
            elif poc.get("status") == "lost":
                exit_reason = "STOP_OUT"

            return DealCloseInfo(
                pnl=pnl,
                exit_reason=exit_reason,
                commission=0.0,
                swap=0.0,
                fee=0.0,
                close_price=close_price,
                close_time=close_time,
                raw_comment=str(poc.get("status", "")),
            )
        except Exception:
            logger.debug("Deriv get_deal_close_info failed for {}", order_id)
            return None

    # ── Positions ────────────────────────────────────────────────────────

    def get_open_positions(self) -> list[PositionInfo]:
        self._require_connection()
        resp = self._sync_send({
            "portfolio": 1,
            "contract_type": ["MULTUP", "MULTDOWN"],
        })
        contracts = resp.get("portfolio", {}).get("contracts", [])
        positions: list[PositionInfo] = []
        for c in contracts:
            cid = str(c.get("contract_id", ""))
            local = self._positions.get(cid, {})
            direction = "BUY" if c.get("contract_type") == "MULTUP" else "SELL"
            positions.append(PositionInfo(
                order_id=cid,
                symbol=local.get("symbol", c.get("symbol", "")),
                direction=direction,
                lots=local.get("lots", 0),
                open_price=float(c.get("buy_price", 0)),
                current_price=float(c.get("bid_price", 0)),
                sl=local.get("sl", 0),
                tp=local.get("tp", 0),
                pnl=float(c.get("profit", 0)),
                swap=0.0,
                open_time=datetime.fromtimestamp(
                    c.get("date_start", 0), tz=timezone.utc
                ),
                platform="deriv",
            ))
        return positions

    def get_position_info(self, order_id: str) -> Optional[PositionInfo]:
        self._require_connection()
        resp = self._sync_send({
            "proposal_open_contract": 1,
            "contract_id": int(order_id),
        })
        if resp.get("error"):
            return None
        poc = resp.get("proposal_open_contract", {})
        local = self._positions.get(order_id, {})
        direction = "BUY" if poc.get("contract_type") == "MULTUP" else "SELL"
        return PositionInfo(
            order_id=order_id,
            symbol=local.get("symbol", poc.get("underlying", "")),
            direction=direction,
            lots=local.get("lots", 0),
            open_price=float(poc.get("buy_price", 0)),
            current_price=float(poc.get("bid_price", 0)),
            sl=local.get("sl", 0),
            tp=local.get("tp", 0),
            pnl=float(poc.get("profit", 0)),
            swap=0.0,
            open_time=datetime.fromtimestamp(
                poc.get("date_start", 0), tz=timezone.utc
            ),
            platform="deriv",
        )

    # ── Mapping ──────────────────────────────────────────────────────────

    def symbol_map(self, apex_symbol: str) -> str:
        return self._mapper.to_broker(apex_symbol)

    def timeframe_map(self, tf: str) -> int:
        return _GRANULARITY_MAP.get(tf.upper(), 3600)

    # ── Private helpers ──────────────────────────────────────────────────

    def _require_connection(self) -> None:
        if self._reconnecting:
            raise ConnectionError("Deriv is reconnecting — request blocked")
        if not self._connected or self._ws is None:
            raise ConnectionError("Deriv is not connected")

    def _persist_position(self, contract_id: str) -> None:
        """Mirror an in-memory contract to the crash-safe store (best effort)."""
        store = getattr(self, "_store", None)
        if store is None or not contract_id:
            return
        data = self._positions.get(contract_id)
        if data is None:
            return
        try:
            store.save_position(contract_id, data)
        except Exception as exc:
            logger.debug("[deriv] persist position {} failed: {}", contract_id, exc)

    def _unpersist_position(self, contract_id: str) -> None:
        """Remove a closed contract from the crash-safe store (best effort)."""
        store = getattr(self, "_store", None)
        if store is None or not contract_id:
            return
        try:
            store.remove_position(contract_id)
        except Exception as exc:
            logger.debug("[deriv] unpersist position {} failed: {}", contract_id, exc)

    def _find_contract_by_idem_key(self, idem_key: str) -> str:
        """Query the Deriv portfolio for a contract matching *idem_key*.

        Returns the contract_id string if found, empty string otherwise.
        Uses the in-memory ``_positions`` dict first (fast path) then
        falls back to a live portfolio query (handles crash restart),
        then checks recent profit table for filled-then-closed contracts.
        """
        for cid, info in self._positions.items():
            if info.get("idem_key") == idem_key:
                return cid
        try:
            resp = self._sync_send({
                "portfolio": 1,
                "contract_type": ["MULTUP", "MULTDOWN"],
            })
            for c in resp.get("portfolio", {}).get("contracts", []):
                pt = c.get("passthrough") or {}
                if pt.get("idem_key") == idem_key:
                    return str(c.get("contract_id", ""))
        except Exception as exc:
            logger.warning("[deriv] idempotency portfolio lookup failed: {}", exc)
            pass
        try:
            import time as _time_mod
            now_epoch = int(_time_mod.time())
            lookback_seconds = 900
            resp = self._sync_send({
                "profit_table": 1,
                "date_from": now_epoch - lookback_seconds,
                "date_to": now_epoch,
                "limit": 50,
                "sort": "DESC",
            })
            for txn in resp.get("profit_table", {}).get("transactions", []):
                pt = txn.get("passthrough") or {}
                if pt.get("idem_key") == idem_key:
                    return str(txn.get("contract_id", txn.get("transaction_id", "")))
        except Exception as exc:
            logger.warning("[deriv] idempotency profit_table lookup failed (non-fatal): {}", exc)
        return ""
