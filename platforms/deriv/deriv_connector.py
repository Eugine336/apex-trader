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
from typing import Any, Optional

import pandas as pd
from loguru import logger

from brain.symbol_mapper import SymbolMapper
from config import get_pip_size
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

# Deriv's `balance` endpoint is strictly rate-limited. Coalesce bursts of
# balance/account-info requests within this TTL into a single API call, and on
# a rate-limit (or other) error serve the last good value up to the max-stale
# age rather than failing every balance-dependent path (entry sizing, margin).
_BALANCE_CACHE_TTL = 3.0   # seconds — within this window, serve cache, no API hit
_BALANCE_MAX_STALE = 90.0  # seconds — serve cached balance through an error storm


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
            self._token_expires_at: float = _time.time() + float(token_expires_in)
        except (TypeError, ValueError):
            self._token_expires_at = _time.time() + 3600.0
        self._token_expiry_warned = False

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
                resp = await self._send(
                    {"contracts_for": sym, "currency": "USD", "product_type": "basic"}
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
            self._req_id += 1
            payload["req_id"] = self._req_id
            for attempt in range(2):  # one retry after reconnect
                try:
                    await self._ws.send(json.dumps(payload))
                    raw = await asyncio.wait_for(self._ws.recv(), timeout=_REQUEST_TIMEOUT)
                    return json.loads(raw)
                except Exception as exc:
                    if attempt == 0:
                        logger.warning("Deriv send error ({}), reconnecting…", exc)
                        ok = await self._reconnect()
                        if not ok:
                            raise ConnectionError("Deriv reconnect failed") from exc
                    else:
                        raise

    def _sync_send(self, payload: dict) -> dict:
        with self._thread_lock:
            if "ticks_history" in payload:
                elapsed = _time.monotonic() - self._last_history_request
                # 0.5s between candle/tick requests — Deriv rate limit is ~3 req/s
                if elapsed < 0.5:
                    _time.sleep(0.5 - elapsed)
                self._last_history_request = _time.monotonic()
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
        if age > self._max_tick_age_seconds:
            logger.warning(
                "Stale tick for {}: {:.1f}s old (limit {}s)",
                mapped, age, self._max_tick_age_seconds,
            )
            raise RuntimeError(
                f"Stale tick for {mapped}: {age:.1f}s old "
                f"(limit {self._max_tick_age_seconds}s)"
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

    def _get_multiplier(self, mapped_symbol: str) -> int:
        """Look up the correct multiplier for a Deriv symbol.
        Prefers runtime-discovered values over static JSON config.
        Snaps the default value to the nearest accepted multiplier so Deriv
        never rejects the order with 'Multiplier is not in acceptable range'.
        """
        _FALLBACK_ACCEPTED = [80, 200, 400, 600, 800, 1000, 2000, 4000]

        if mapped_symbol in self._discovered_multipliers:
            accepted = self._discovered_multipliers[mapped_symbol]
        else:
            accepted = None

        try:
            cfg_path = Path(__file__).resolve().parent.parent.parent / "config" / "brokers" / "deriv.json"
            with open(cfg_path) as f:
                cfg = json.load(f)
            mult_map = cfg.get("multipliers", {})
            entry = mult_map.get(mapped_symbol) or mult_map.get("_default", {})
            desired = int(entry.get("default", 1000))
            if accepted is None:
                accepted = [int(x) for x in entry.get("accepted", _FALLBACK_ACCEPTED)]
        except Exception:
            desired = 1000
            if accepted is None:
                accepted = _FALLBACK_ACCEPTED

        if not accepted:
            accepted = _FALLBACK_ACCEPTED
        nearest = min(accepted, key=lambda x: abs(x - desired))
        return nearest

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

        t0 = _time.monotonic()
        buy_payload: dict = {
            "buy": 1,
            "subscribe": 1,
            "price": amount,
            "parameters": {
                "contract_type": contract_type,
                "symbol": mapped,
                "currency": "USD",
                "amount": amount,
                "basis": "stake",
                "multiplier": multiplier,
                "limit_order": {
                    "stop_loss": round(abs(price - sl) / price * amount * multiplier, 2),
                    "take_profit": round(abs(tp - price) / price * amount * multiplier, 2),
                },
            },
        }
        if passthrough:
            buy_payload["passthrough"] = passthrough
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
        sl_pct = abs(price - sl) / price if price > 0 else 0
        tp_pct = abs(tp - price) / price if price > 0 else 0
        err: Optional[str] = resp["error"].get("message", "Unknown error") if resp.get("error") else None

        for _attempt in range(MAX_RETRIES):
            if err is None:
                break

            changed = False
            # SAFETY: never open a Deriv position without its protective stop.
            # Previously a parameters-validation error stripped the limit_order
            # (SL+TP) and re-sent the trade NAKED while still recording it as
            # protected. Fail closed instead — skipping a trade is always safer
            # than holding an unprotected position.
            if (
                "Input validation failed: parameters" in err
                and "limit_order" in buy_payload["parameters"]
            ):
                logger.error(
                    "Deriv rejected limit_order (SL/TP) params for {} — FAILING CLOSED "
                    "(refusing to open a naked position). Check SL/TP bounds in config.",
                    mapped,
                )
                break

            # ── 1. Multiplier correction (always fix this first) ───────────
            _mult_match = _re.search(
                r"Multiplier is not in acceptable range.*?Accepts\s+([\d,\s]+)", err
            )
            if _mult_match:
                valid = sorted(int(x.strip()) for x in _mult_match.group(1).split(",") if x.strip().isdigit())
                if valid:
                    corrected = min(valid, key=lambda x: abs(x - multiplier))
                    logger.warning(
                        "Deriv multiplier {} rejected for {} — retrying with {} (valid: {}). "
                        "Update config/brokers/deriv.json!",
                        multiplier, mapped, corrected, valid,
                    )
                    self._discovered_multipliers[mapped] = valid
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

            if not changed:
                break

            # Recompute SL/TP dollar values from current amount so the cap
            # does not slide due to a stale, oversized stop_loss value.
            sl_dollar = round(sl_pct * amount * multiplier, 2)
            tp_dollar = round(tp_pct * amount * multiplier, 2)

            resp = self._sync_send({
                "buy": 1,
                "subscribe": 1,
                "price": amount,
                "parameters": {
                    "contract_type": contract_type,
                    "symbol": mapped,
                    "currency": "USD",
                    "amount": amount,
                    "basis": "stake",
                    "multiplier": multiplier,
                    "limit_order": {
                        "stop_loss": sl_dollar,
                        "take_profit": tp_dollar,
                    },
                },
            })
            err = resp["error"].get("message", "Unknown error") if resp.get("error") else None


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
            if new_sl is not None:
                limit_order["stop_loss"] = round(abs(open_price - new_sl) / open_price * stake * multiplier, 2)
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
