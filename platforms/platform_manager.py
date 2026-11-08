"""
APEX TRADER — Unified Platform Manager
One interface to rule them all.
Routes trades to the right platform based on instrument type.

MT5: supports multiple simultaneous broker accounts via MT5_BROKERS in .env.
     Each broker gets its own MT5Connector and handles whatever symbols its
     broker config covers. Symbol routing tries brokers in order, picking the
     first one that has the symbol available.

Deriv: single connection (WebSocket API is account-scoped).
"""

import json
import math
import os
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger as _pm_logger

import pandas as pd

logger = _pm_logger

from config import (
    AppConfig,
    Platform,
    get_instrument,
)
from brain.symbol_mapper import resolve_to_internal
from platforms.base_connector import (
    AccountInfo,
    BaseConnector,
    CloseResult,
    DealCloseInfo,
    OrderResult,
    PositionInfo,
    TickData,
)
from platforms.deriv.deriv_connector import DerivConnector
from platforms.mt5.mt5_connector import MT5Connector


_DEFAULT_DERIV_TOKEN_TTL_SECONDS = 3600.0


def _deriv_token_ttl_from_env() -> float:
    """Return a validated Deriv access-token TTL, defaulting on blank/invalid input."""
    raw = os.getenv("DERIV_TOKEN_EXPIRES_IN", "").strip()
    if not raw:
        return _DEFAULT_DERIV_TOKEN_TTL_SECONDS

    try:
        ttl = float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid DERIV_TOKEN_EXPIRES_IN; using the 3600-second default")
        return _DEFAULT_DERIV_TOKEN_TTL_SECONDS

    if not math.isfinite(ttl) or ttl <= 0:
        logger.warning("DERIV_TOKEN_EXPIRES_IN must be a finite positive number; using the 3600-second default")
        return _DEFAULT_DERIV_TOKEN_TTL_SECONDS
    return ttl


@dataclass
class BrokerPositionsSnapshot:
    """Result of fetching broker positions with per-platform confirmation.

    Callers must check ``confirmed_platforms`` before inferring that a
    position's absence means it was closed.  A platform in
    ``failed_platforms`` returned no data due to an error — its positions
    are *unknown*, not absent.
    """
    positions: list = field(default_factory=list)
    confirmed_platforms: set = field(default_factory=set)
    failed_platforms: set = field(default_factory=set)


def _load_mt5_configs() -> list[dict]:
    """
    Load MT5 broker credentials from environment variables.

    Supports two formats:

    1. Multi-broker JSON array (MT5_BROKERS):
       MT5_BROKERS='[{"login":12345,"password":"pw","server":"Broker-Live"},
                     {"login":67890,"password":"pw2","server":"OtherBroker-Live"}]'

    2. Single-broker legacy vars (MT5_LOGIN / MT5_PASSWORD / MT5_SERVER):
       MT5_LOGIN=12345
       MT5_PASSWORD=pw
       MT5_SERVER=Broker-Live

    MT5_BROKERS takes priority if both are set.
    Returns a list of dicts with keys: login (int), password (str), server (str).
    """
    raw = os.getenv("MT5_BROKERS", "").strip()
    if raw:
        try:
            brokers = json.loads(raw)
            if isinstance(brokers, list) and brokers:
                return [
                    {
                        "login": int(b.get("login", 0)),
                        "password": str(b.get("password", "")),
                        "server": str(b.get("server", "")),
                    }
                    for b in brokers
                ]
        except Exception as exc:
            logger.error("MT5_BROKERS JSON parse failed — falling back to MT5_LOGIN: {}", exc)

    # Legacy single-broker vars
    login = int(os.getenv("MT5_LOGIN", "0"))
    password = os.getenv("MT5_PASSWORD", "")
    server = os.getenv("MT5_SERVER", "")
    return [{"login": login, "password": password, "server": server}]


class PlatformManager:
    """Routes every operation to the correct platform connector."""

    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or AppConfig()
        # Attached by SystemContext; execution telemetry is never an admission control.
        self.execution_monitor = None
        # EventDrivenSystem installs this callback to maintain its durable pending-order projection.
        self.pending_order_callback = None

        # ── Intraday candle cache (Phase 5): transparent TTL cache in front of
        # per-symbol/timeframe broker fetches. See platforms/candle_cache.py. ─
        from platforms.candle_cache import CandleCache
        _perf = getattr(self.config, "performance", None)
        self.candle_cache = CandleCache(
            ttl_by_tf=getattr(_perf, "candle_cache_ttl", None),
            default_ttl=getattr(_perf, "candle_cache_default_ttl", 5.0),
            enabled=getattr(_perf, "candle_cache_enabled", True),
        )

        # ── MT5: one connector per broker account ────────────────────────
        mt5_configs = _load_mt5_configs()
        self.mt5_connectors: list[MT5Connector] = [
            MT5Connector(
                login=cfg["login"],
                password=cfg["password"],
                server=cfg["server"],
                deviation=self.config.risk.max_deviation_points,
                reject_on_minlot_inflation=self.config.risk.reject_on_minlot_inflation,
                max_tick_age_seconds=self.config.risk.max_tick_age_seconds,
                max_slippage_pips=getattr(self.config.risk, "max_slippage_pips", 0.0),
                min_rr_after_adjust=self.config.risk.min_risk_reward,
            )
            for cfg in mt5_configs
        ]
        # Backwards-compat: self.mt5 points to the first (primary) connector.
        # Existing code that does `manager.mt5.something` keeps working.
        self.mt5: MT5Connector = self.mt5_connectors[0]

        # ── Deriv: single WebSocket connection (OAuth2 + OTP auth) ───────
        self.deriv = DerivConnector(
            client_id=os.getenv("DERIV_CLIENT_ID", ""),
            access_token=os.getenv("DERIV_ACCESS_TOKEN", ""),
            account_type=os.getenv("DERIV_ACCOUNT_TYPE", "demo"),
            token_expires_in=_deriv_token_ttl_from_env(),
            app_id=os.getenv("DERIV_APP_ID", ""),
            token_refresh_callback=self._refresh_deriv_token,
            max_tick_age_seconds=self.config.risk.max_tick_age_seconds,
            reconnect_max_attempts=getattr(self.config.ops, "reconnect_max_retries", 10),
            reconnect_base_delay=getattr(self.config.ops, "reconnect_base_delay_seconds", 5.0),
        )

        self._mt5_connected_flags: list[bool] = [False] * len(self.mt5_connectors)
        self._deriv_connected = False
        # Last-known good balance per platform key. Each platform's balance is
        # tracked INDEPENDENTLY so a disconnected leg keeps contributing its
        # last value to the pooled total instead of vanishing — otherwise the
        # pooled balance oscillates wildly (and trips the risk engine's swing
        # guard) every time one broker reconnects.
        self._last_known_balances: dict[str, float] = {}

        self._mt5_was_connected: list[bool] = [False] * len(self.mt5_connectors)
        self._deriv_was_connected = False
        # Exponential backoff schedule for proactive reconnects, derived from
        # OpsConfig.reconnect_base_delay_seconds so the cadence is configurable
        # (default base 5.0 → [5, 10, 20, 40, 60], the historical schedule).
        _base = float(getattr(self.config.ops, "reconnect_base_delay_seconds", 5.0) or 5.0)
        self._reconnect_delays = [max(1.0, _base * mult) for mult in (1, 2, 4, 8, 12)]
        self._mt5_reconnect_attempts: list[int] = [0] * len(self.mt5_connectors)
        self._deriv_reconnect_attempt = 0
        self._mt5_next_reconnects: list[float] = [0.0] * len(self.mt5_connectors)
        self._deriv_next_reconnect: float = 0.0

        self._deriv_reconnect_warned: bool = False

        # Cache of parsed broker symbol-override sets keyed by broker name. The
        # broker JSON config is static for the process lifetime, so it is read
        # and parsed once per broker instead of on every get_connector() call
        # (which is hit many times per position per cycle). Connectivity is
        # still checked live against _mt5_connected_flags, so routing stays
        # correct across disconnects/reconnects.
        self._broker_overrides_cache: dict[str, Optional[set]] = {}
        # Read-only broker-native symbol catalog; discovery never grants trade permission.
        self._mt5_symbol_catalog: list[dict] = []

        # Per-cycle price snapshot (thread-local). When the trading loop opens a
        # snapshot for its management pass, repeated get_price() calls for the
        # same symbol within that thread return the SAME tick — every open
        # position is evaluated against one consistent price point per cycle,
        # and each unique symbol is fetched from the broker at most once
        # (collapsing O(positions × passes) round-trips to O(unique symbols)).
        # Thread-local so the dashboard and other threads always read live
        # prices and never share the loop's snapshot.
        self._price_snapshot = threading.local()

        # Single-writer broker mutex: every broker MUTATION (entry, pending,
        # modify, close) acquires this re-entrant lock so no two threads can
        # issue concurrent broker calls. The MT5 API is not thread-safe, and
        # the entry path runs on the tick thread while management runs on the
        # flush thread — without this they could race on the same connection.
        self._broker_write_lock = threading.RLock()

    # ── Convenience properties ───────────────────────────────────────────

    @property
    def _mt5_connected(self) -> bool:
        """True if at least one MT5 broker is online."""
        return any(self._mt5_connected_flags)

    @_mt5_connected.setter
    def _mt5_connected(self, value: bool) -> None:
        """Legacy setter — sets all brokers to the same state (used by tests)."""
        self._mt5_connected_flags = [value] * len(self.mt5_connectors)

    # ── Connection management ────────────────────────────────────────────

    def connect_all(self) -> dict[str, bool]:
        results: dict[str, bool] = {}

        for i, connector in enumerate(self.mt5_connectors):
            label = f"MT5[{i}]" if len(self.mt5_connectors) > 1 else "MT5"
            logger.info("Connecting to {}…", label)
            ok = connector.connect()
            self._mt5_connected_flags[i] = ok
            self._mt5_was_connected[i] = ok
            results[f"mt5_{i}"] = ok
            if ok:
                logger.info("{} — ONLINE ({})", label, getattr(connector, "_broker_name", ""))
            else:
                logger.warning("{} — OFFLINE (non-Windows or credentials missing)", label)

        # Legacy key so callers checking results.get("mt5") still work
        results["mt5"] = self._mt5_connected

        self._mt5_symbol_catalog = self.discover_mt5_symbols(refresh=True)
        eligible_symbols = list(dict.fromkeys(
            str(row.get("apex_symbol", "")).upper()
            for row in self._mt5_symbol_catalog
            if row.get("new_position_eligible") and row.get("apex_symbol")
        ))
        self.config.broker_discovery_complete = self._mt5_connected
        self.config.broker_enabled_symbols = eligible_symbols
        eligible_count = len(eligible_symbols)
        logger.info(
            "[universe] MT5 discovered {} broker symbols; {} map to supported Apex instruments with complete specs",
            len(self._mt5_symbol_catalog), eligible_count,
        )

        logger.info("Connecting to Deriv…")
        self._deriv_connected = self.deriv.connect()
        results["deriv"] = self._deriv_connected
        if self._deriv_connected:
            logger.info("Deriv — ONLINE")
        else:
            logger.warning("Deriv — OFFLINE (credentials missing or connection failed)")

        if not any(results.values()):
            logger.error("No platforms connected — trading disabled")

        self._deriv_was_connected = self._deriv_connected
        return results

    def disconnect_all(self) -> None:
        for connector in self.mt5_connectors:
            connector.disconnect()
        self.deriv.disconnect()
        self._mt5_connected_flags = [False] * len(self.mt5_connectors)
        self._deriv_connected = False
        logger.info("All platforms disconnected")

    def check_connections(self) -> dict[str, bool]:
        """Return live connection state for each platform."""
        mt5_live = False
        for i, connector in enumerate(self.mt5_connectors):
            if self._mt5_connected_flags[i]:
                try:
                    alive = connector.is_connected()
                except Exception:
                    alive = False
                self._mt5_connected_flags[i] = alive
                if alive:
                    mt5_live = True

        deriv_live = False
        if self._deriv_connected:
            try:
                deriv_live = self.deriv.is_connected()
            except Exception:
                deriv_live = False
            if not deriv_live:
                self._deriv_connected = False

        return {"mt5": mt5_live, "deriv": deriv_live}

    def reconnect_platform(self, platform: str) -> bool:
        """Attempt to reconnect a platform with exponential backoff.

        platform can be "mt5" (reconnects all offline MT5 brokers),
        "mt5_0", "mt5_1" etc. (reconnects a specific broker), or "deriv".
        """
        delays = self._reconnect_delays
        max_attempts = len(delays)

        if platform.startswith("mt5"):
            # Determine which broker indices to reconnect
            if "_" in platform and platform != "mt5":
                try:
                    indices = [int(platform.split("_")[1])]
                except (IndexError, ValueError):
                    indices = list(range(len(self.mt5_connectors)))
            else:
                indices = list(range(len(self.mt5_connectors)))

            any_success = False
            for i in indices:
                if self._mt5_connected_flags[i]:
                    continue  # already up
                attempt = self._mt5_reconnect_attempts[i]
                # Never permanently give up — clamp to the longest backoff and
                # keep retrying so a long outage self-heals without a restart.
                delay = delays[min(attempt, max_attempts - 1)]
                label = f"MT5[{i}]" if len(self.mt5_connectors) > 1 else "MT5"
                logger.warning(
                    "{} reconnect attempt {} (backoff {}s)",
                    label, attempt + 1, delay,
                )
                try:
                    success = self.mt5_connectors[i].connect()
                except Exception as exc:
                    logger.error("{} reconnect error: {}", label, exc)
                    success = False

                if success:
                    self._mt5_connected_flags[i] = True
                    self._mt5_reconnect_attempts[i] = 0
                    self._mt5_next_reconnects[i] = 0.0
                    logger.info("{} RECONNECTED", label)
                    any_success = True
                else:
                    self._mt5_reconnect_attempts[i] = attempt + 1
                    self._mt5_next_reconnects[i] = _time.monotonic() + delay
            return any_success

        else:  # deriv
            attempt = self._deriv_reconnect_attempt
            # Never permanently give up — clamp to the longest backoff and keep
            # retrying so a long outage self-heals without a restart.
            delay = delays[min(attempt, max_attempts - 1)]
            logger.warning(
                "DERIV reconnect attempt {} (backoff {}s)",
                attempt + 1, delay,
            )
            try:
                success = self.deriv.connect()
            except Exception as exc:
                logger.error("DERIV reconnect error: {}", exc)
                success = False

            if success:
                self._deriv_connected = True
                self._deriv_reconnect_attempt = 0
                self._deriv_next_reconnect = 0.0
                logger.info("DERIV RECONNECTED")
                return True

            self._deriv_reconnect_attempt = attempt + 1
            self._deriv_next_reconnect = _time.monotonic() + delay
            return False

    def should_attempt_reconnect(self, platform: str) -> bool:
        """Check if enough time has passed to attempt reconnection (non-blocking)."""
        now = _time.monotonic()
        if platform.startswith("mt5"):
            if self._mt5_connected:
                return False
            if not any(self._mt5_was_connected):
                return False
            return now >= min(self._mt5_next_reconnects)
        else:
            if self._deriv_connected:
                return False
            if not self._deriv_was_connected:
                return False
            return now >= self._deriv_next_reconnect

    @property
    def any_connected(self) -> bool:
        return self._mt5_connected or self._deriv_connected

    # ── Routing ──────────────────────────────────────────────────────────

    def get_connector(self, symbol: str) -> BaseConnector:
        """Route a symbol to the correct platform connector.

        For MT5 symbols, tries each connected broker in order and returns
        the first one whose broker config has a mapping for the symbol.
        Falls back to any connected MT5 if none have an explicit mapping.
        """
        try:
            info = get_instrument(symbol)
        except KeyError:
            # Unknown symbol — fall back to first available
            if self._mt5_connected:
                return self._first_connected_mt5()
            if self._deriv_connected:
                return self.deriv
            raise ConnectionError(f"No platform available for {symbol}")

        if info.platform == Platform.DERIV:
            if self._deriv_connected:
                return self.deriv
            raise ConnectionError(f"Deriv not connected for {symbol}")

        if info.platform == Platform.MT5:
            connector = self._route_mt5_symbol(symbol)
            if connector:
                return connector
            raise ConnectionError(f"No MT5 broker connected for {symbol}")

        # Platform.BOTH — prefer MT5, fall back to Deriv
        connector = self._route_mt5_symbol(symbol)
        if connector:
            return connector
        if self._deriv_connected:
            return self.deriv
        raise ConnectionError(f"No platform connected for {symbol}")

    def _broker_overrides(self, broker_name: str) -> Optional[set]:
        """Return the set of symbol-override keys for a broker, parsed once.

        Caches the parsed result (including a None sentinel for missing/invalid
        configs) so the broker JSON is never re-read from disk on the hot path.
        """
        if broker_name in self._broker_overrides_cache:
            return self._broker_overrides_cache[broker_name]
        from pathlib import Path

        result: Optional[set] = None
        cfg_path = (
            Path(__file__).parent.parent / "config" / "brokers" / f"{broker_name}.json"
        )
        if cfg_path.exists():
            try:
                with open(cfg_path) as f:
                    cfg = json.load(f)
                overrides = cfg.get("overrides", {})
                result = {str(k) for k in overrides}
                result |= {str(k).upper() for k in overrides}
            except Exception as exc:
                logger.warning("[platform_manager] config load for symbol routing failed: {}", exc)
                result = None
        self._broker_overrides_cache[broker_name] = result
        return result

    def _route_mt5_symbol(self, symbol: str) -> Optional[MT5Connector]:
        """Return the best connected MT5 connector for a symbol.

        Preference order:
        1. A connected broker that has an explicit mapping for the symbol
        2. Any connected broker (first one wins)
        """
        fallback: Optional[MT5Connector] = None

        for i, connector in enumerate(self.mt5_connectors):
            if not self._mt5_connected_flags[i]:
                continue
            if fallback is None:
                fallback = connector

            # Check if broker config has an explicit mapping for this symbol
            broker_name = getattr(connector, "_broker_name", "")
            if not broker_name or broker_name == "auto":
                continue
            overrides = self._broker_overrides(broker_name)
            if not overrides:
                continue
            if symbol in overrides or symbol.upper() in overrides:
                return connector

        return fallback  # None if no MT5 brokers connected

    def _first_connected_mt5(self) -> MT5Connector:
        for i, connector in enumerate(self.mt5_connectors):
            if self._mt5_connected_flags[i]:
                return connector
        raise ConnectionError("No MT5 broker connected")

    def get_platform_name(self, symbol: str) -> str:
        connector = self.get_connector(symbol)
        return "mt5" if isinstance(connector, MT5Connector) else "deriv"

    def get_broker_name(self, symbol: str) -> str:
        """Return a human-readable broker identifier for the platform serving this symbol."""
        connector = self.get_connector(symbol)
        if isinstance(connector, MT5Connector):
            broker_name = getattr(connector, "_broker_name", "")
            if broker_name and broker_name != "auto":
                return broker_name
            server = getattr(connector, "_server", "") or ""
            return server.split("-")[0].lower() if server else "mt5"
        return "deriv"

    def get_account_id(self, symbol: str) -> str:
        """Stable per-account identifier (login / loginid) for the connector
        serving this symbol.

        Used to keep two logins on the SAME broker in separate risk silos.
        Reads the connector's stored id directly — no broker round-trip.
        """
        try:
            connector = self.get_connector(symbol)
        except Exception:
            return ""
        if isinstance(connector, MT5Connector):
            login = getattr(connector, "_login", 0)
            return str(login) if login else ""
        if isinstance(connector, DerivConnector):
            return str(getattr(connector, "_account_id", "") or "")
        return ""

    def discover_mt5_symbols(self, *, refresh: bool = False) -> list[dict]:
        """Return the current broker-native MT5 symbol catalog.

        Unsupported and disabled symbols remain visible with explicit flags.
        Owner selection, feed capability and risk admission are separate.
        """
        if refresh:
            catalog: list[dict] = []
            for index, connector in enumerate(self.mt5_connectors):
                if index >= len(self._mt5_connected_flags) or not self._mt5_connected_flags[index]:
                    continue
                try:
                    for row in connector.discover_symbols():
                        catalog.append({**row, "connector_index": index})
                except Exception as exc:
                    _pm_logger.warning("[universe] MT5 symbol discovery failed for connector {}: {}", index, exc)
            self._mt5_symbol_catalog = catalog
        return [dict(row) for row in self._mt5_symbol_catalog]

    def get_typical_spreads(self, symbol: str) -> dict[str, float]:
        from config import INSTRUMENT_REGISTRY
        result: dict[str, float] = {}
        for sym, info in INSTRUMENT_REGISTRY.items():
            spread = getattr(info, "typical_spread_pips", 0.0)
            if spread > 0:
                result[sym.upper()] = spread
        return result

    # ── Trade execution ──────────────────────────────────────────────────

    def find_order_by_idem_key(self, idem_key: str):
        """Search all connected platforms for an order matching *idem_key*."""
        for i, conn in enumerate(self.mt5_connectors):
            if self._mt5_connected_flags[i]:
                try:
                    result = conn._find_order_by_idem_key(idem_key)
                    if result is not None:
                        return result
                except Exception as exc:
                    _pm_logger.warning("[pm] MT5 idem-key lookup failed: {}", exc)
        if self._deriv_connected:
            try:
                cid = self.deriv._find_contract_by_idem_key(idem_key)
                if cid:
                    return cid
            except Exception as exc:
                _pm_logger.warning("[pm] Deriv idem-key lookup failed: {}", exc)
        return None

    def set_execution_monitor(self, monitor: Optional[object]) -> None:
        """Attach the shared execution telemetry sink; never changes admission."""
        self.execution_monitor = monitor

    def execute_pending_entry(
        self,
        symbol: str,
        direction: str,
        order_type: str,
        entry_price: float,
        lots: float,
        sl: float,
        tp: float,
        comment: str = "",
        idempotency_key: str = "",
        decision_id: str = "",
        campaign_id: str = "",
        opportunity_id: str = "",
        decision_feature_snapshot: Optional[dict] = None,
    ) -> OrderResult:
        """Submit a Brain-authorized pending entry through the MT5 gateway only."""
        try:
            from persistence.event_store import get_existing_event_store
            lifecycle_store = get_existing_event_store()
        except Exception as exc:  # noqa: BLE001 — pending order must be durably tracked before broker mutation
            return OrderResult(
                success=False, order_id="", fill_price=0.0,
                requested_price=float(entry_price or 0.0), slippage_pips=0.0,
                lots=float(lots or 0.0), symbol=symbol,
                direction=str(direction or "").upper(), sl=float(sl or 0.0),
                tp=float(tp or 0.0), platform="mt5",
                error=f"pending_order_lifecycle_store_unavailable: {type(exc).__name__}",
                order_status="REJECTED", order_type=str(order_type or "").upper(),
            )
        if lifecycle_store is None or bool(getattr(lifecycle_store, "is_degraded", False)):
            return OrderResult(
                success=False, order_id="", fill_price=0.0,
                requested_price=float(entry_price or 0.0), slippage_pips=0.0,
                lots=float(lots or 0.0), symbol=symbol,
                direction=str(direction or "").upper(), sl=float(sl or 0.0),
                tp=float(tp or 0.0), platform="mt5",
                error="pending_order_lifecycle_store_unavailable_or_degraded",
                order_status="REJECTED", order_type=str(order_type or "").upper(),
            )
        connector = self.get_connector(symbol)
        if not isinstance(connector, MT5Connector):
            return OrderResult(
                success=False, order_id="", fill_price=0.0,
                requested_price=float(entry_price or 0.0), slippage_pips=0.0,
                lots=float(lots or 0.0), symbol=symbol,
                direction=str(direction or "").upper(), sl=float(sl or 0.0),
                tp=float(tp or 0.0), platform="unsupported",
                error="pending_orders_unsupported_on_selected_platform",
                order_status="REJECTED", order_type=str(order_type or "").upper(),
            )
        with self._broker_write_lock:
            result = connector.place_pending_order(
                symbol=symbol, direction=direction, order_type=order_type,
                entry_price=entry_price, lots=lots, sl=sl, tp=tp,
                comment=comment, idempotency_key=idempotency_key,
            )
        trackable = (
            bool(result.success and result.order_status in {"PENDING", "PARTIALLY_FILLED_PENDING"} and result.order_id)
            or (
                str(result.error or "").upper().startswith("AMBIGUOUS_PENDING_ORDER_ACK")
                and bool(idempotency_key)
            )
        )
        if trackable:
            lifecycle_context = {
                "order_id": str(result.order_id or ""),
                "tracking_key": str(result.order_id or idempotency_key),
                "order_type": str(order_type or "").upper(),
                "direction": str(direction or "").upper(),
                "lots": float(lots),
                "remaining_lots": float(result.lots or lots),
                "entry_price": float(result.requested_price or entry_price),
                "sl": float(result.sl or sl), "tp": float(result.tp or tp),
                "platform": str(result.platform or "mt5"),
                "idempotency_key": idempotency_key,
                "submitted_at": result.timestamp.astimezone(timezone.utc).isoformat(),
                "decision_id": decision_id, "campaign_id": campaign_id,
                "opportunity_id": opportunity_id,
                "decision_feature_snapshot": (
                    dict(decision_feature_snapshot)
                    if isinstance(decision_feature_snapshot, dict) else None
                ),
                "status": (
                    str(result.order_status or "UNKNOWN").upper()
                    if result.success else "UNKNOWN"
                ),
                "broker_confirmed": bool(
                    result.success and result.order_status in {"PENDING", "PARTIALLY_FILLED_PENDING"}
                ),
                "reconciliation_required": not bool(result.success),
            }
            try:
                from persistence import domain_events as DE
                from persistence.event_store import get_event_store
                store = get_event_store()
                if store is not None:
                    store.emit(
                        event_type=DE.ORDER_PENDING,
                        severity="INFO" if result.success else "WARNING",
                        symbol=symbol, correlation_id=idempotency_key or None,
                        parent_id=result.order_id or idempotency_key,
                        source_module="platforms.platform_manager", durable=True,
                        payload=lifecycle_context,
                    )
            except Exception as exc:  # noqa: BLE001 — accepted/ambiguous send must be reconciled, never resubmitted
                _pm_logger.critical("[orders] pending order {} lifecycle event persistence failed: {}", result.order_id or idempotency_key, exc)
            callback = getattr(self, "pending_order_callback", None)
            if callable(callback):
                try:
                    callback(result, lifecycle_context)
                except Exception as exc:  # noqa: BLE001 — callback failure leaves durable event for restart restore
                    _pm_logger.critical("[orders] pending order projection callback failed for {}: {}", result.order_id or idempotency_key, exc)
        return result

    def cancel_pending_order(self, order_id: str, *, symbol: str = "") -> bool:
        """Cancel only a uniquely broker-confirmed pending order across MT5 accounts."""
        ticket = str(order_id or "").strip()
        canonical_symbol = str(symbol or "").strip().upper()
        if not ticket or not canonical_symbol:
            return False
        if (
            not self.mt5_connectors
            or len(self._mt5_connected_flags) != len(self.mt5_connectors)
            or not all(self._mt5_connected_flags)
        ):
            _pm_logger.error("[orders] pending cancel refused: MT5 account set is incomplete")
            return False
        matches: list[tuple[int, MT5Connector, dict]] = []
        for index, connector in enumerate(self.mt5_connectors):
            if index >= len(self._mt5_connected_flags) or not self._mt5_connected_flags[index]:
                continue
            try:
                snapshots = connector.get_pending_order_snapshots()
            except Exception as exc:
                _pm_logger.error("[orders] cannot prove pending order book for MT5 account {}: {}", index, exc)
                return False
            for row in snapshots:
                if (
                    str(row.get("ticket", "")) == ticket
                    and resolve_to_internal(str(row.get("symbol", "")).strip().upper()) == canonical_symbol
                    and row.get("source_verified") is True
                ):
                    matches.append((index, connector, row))
        if len(matches) != 1:
            _pm_logger.error("[orders] pending cancel refused: ticket {} symbol {} matched {} verified accounts", ticket, canonical_symbol, len(matches))
            return False
        index, connector, row = matches[0]
        with self._broker_write_lock:
            cancelled = connector.cancel_pending_order(ticket)
        if cancelled:
            try:
                from persistence import domain_events as DE
                from persistence.event_store import get_event_store
                store = get_event_store()
                if store is not None:
                    store.emit(
                        event_type=DE.ORDER_CANCELLED, severity="INFO", symbol=canonical_symbol,
                        parent_id=ticket, source_module="platforms.platform_manager", durable=True,
                        payload={"order_id": ticket, "symbol": canonical_symbol,
                                 "connector_index": index, "status": "CANCELLED",
                                 "pre_cancel_broker_snapshot": row},
                    )
            except Exception as exc:  # noqa: BLE001 — cancellation already happened
                _pm_logger.critical("[orders] pending order {} cancelled but lifecycle event persistence failed: {}", ticket, exc)
        return bool(cancelled)

    def reconcile_pending_order(
        self, order_id: str = "", *, symbol: str, submitted_at: datetime,
        idempotency_key: str = "",
    ) -> dict:
        """Resolve a pending order only from a source-verified live or historical broker record.

        Absence from the live order book is never interpreted as cancellation or fill.
        """
        ticket = str(order_id or "").strip()
        canonical_symbol = str(symbol or "").strip().upper()
        idem_key = str(idempotency_key or "").strip()
        if (
            (not ticket and not idem_key) or not canonical_symbol
            or not isinstance(submitted_at, datetime)
            or submitted_at.tzinfo is None or submitted_at.utcoffset() is None
        ):
            return {"status": "UNKNOWN", "reason": "invalid_reconciliation_identity_or_time", "broker_confirmed": False}
        if (
            len(self._mt5_connected_flags) != len(self.mt5_connectors)
            or not all(self._mt5_connected_flags)
        ):
            return {
                "status": "UNKNOWN",
                "reason": "mt5_account_set_incomplete",
                "broker_confirmed": False,
            }
        if not ticket:
            discoveries: list[tuple[int, Any]] = []
            discovery_failures: list[int] = []
            for index, connector in enumerate(self.mt5_connectors):
                if index >= len(self._mt5_connected_flags) or not self._mt5_connected_flags[index]:
                    continue
                try:
                    finder = getattr(connector, "find_pending_order_by_idempotency_key", None)
                    if not callable(finder):
                        finder = getattr(connector, "find_order_by_idempotency_key", None)
                    candidate = (
                        finder(idem_key, since=submitted_at.astimezone(timezone.utc))
                        if callable(finder) else None
                    )
                    candidate_symbol = str(getattr(candidate, "symbol", "") or "").strip().upper()
                    if candidate is not None and candidate_symbol == canonical_symbol:
                        discoveries.append((index, candidate))
                except Exception:
                    discovery_failures.append(index)
            if discovery_failures:
                return {"status": "UNKNOWN", "reason": "idempotency_lookup_incomplete", "failed_accounts": discovery_failures, "broker_confirmed": False}
            if len(discoveries) > 1:
                return {"status": "AMBIGUOUS", "reason": "idempotency_key_matches_multiple_mt5_accounts", "broker_confirmed": False}
            if not discoveries:
                return {"status": "UNKNOWN", "reason": "idempotency_key_not_found_in_broker_history_or_live_book", "broker_confirmed": False}
            candidate_index, candidate = discoveries[0]
            ticket = str(getattr(candidate, "ticket", "") or "").strip()
            candidate_status = str(getattr(candidate, "order_status", "UNKNOWN") or "UNKNOWN").upper()
            if candidate_status == "FILLED":
                candidate_record = {
                    "ticket": ticket, "position_id": str(getattr(candidate, "position_id", "") or ""),
                    "symbol": canonical_symbol, "state": getattr(candidate, "state", 4),
                    "connector_index": candidate_index,
                    "source": "mt5.idempotency_lookup", "source_verified": True,
                }
                return {"status": "FILLED", "order_id": ticket, "broker_confirmed": True, "record": candidate_record}
        now = datetime.now(timezone.utc)
        if submitted_at.astimezone(timezone.utc) > now:
            return {"status": "UNKNOWN", "reason": "submission_time_in_future", "broker_confirmed": False}
        live_matches: list[dict] = []
        history_matches: list[dict] = []
        failed_accounts: list[int] = []
        for index, connector in enumerate(self.mt5_connectors):
            if index >= len(self._mt5_connected_flags) or not self._mt5_connected_flags[index]:
                continue
            try:
                live = connector.get_pending_order_snapshots()
                live_matches.extend(
                    {**row, "connector_index": index}
                    for row in live
                    if str(row.get("ticket", "")) == ticket
                    and str(row.get("symbol", "")).strip().upper() == canonical_symbol
                    and row.get("source_verified") is True
                )
                history = connector.get_order_history(submitted_at.astimezone(timezone.utc), now)
                history_matches.extend(
                    {**row, "connector_index": index}
                    for row in history
                    if str(row.get("ticket", "")) == ticket
                    and str(row.get("symbol", "")).strip().upper() == canonical_symbol
                    and row.get("source_verified") is True
                )
            except Exception:
                failed_accounts.append(index)
        if len(live_matches) == 1:
            record = live_matches[0]
            try:
                initial = float(record.get("volume_initial", record.get("volume", 0.0)) or 0.0)
                remaining = float(record.get("volume_current", record.get("volume", 0.0)) or 0.0)
            except (TypeError, ValueError, OverflowError):
                initial = remaining = 0.0
            status = (
                "PARTIALLY_FILLED_PENDING"
                if initial > 0.0 and 0.0 < remaining < initial
                else "PENDING"
            )
            return {"status": status, "order_id": ticket, "broker_confirmed": True, "record": record,
                    "initial_volume": initial, "remaining_volume": remaining}
        if len(live_matches) > 1 or len(history_matches) > 1:
            return {"status": "AMBIGUOUS", "reason": "ticket_matches_multiple_broker_accounts_or_history_rows", "broker_confirmed": False}
        if failed_accounts:
            return {"status": "UNKNOWN", "order_id": ticket, "reason": "broker_history_or_live_book_incomplete", "failed_accounts": failed_accounts, "broker_confirmed": False}
        if len(history_matches) == 1:
            record = history_matches[0]
            state = getattr(record.get("state"), "value", record.get("state"))
            state_name = str(state).upper()
            try:
                from platforms.mt5 import mt5_connector as mt5_adapter
                mt5_constants = getattr(mt5_adapter, "mt5", None)
            except Exception:
                mt5_constants = None
            state_map = {
                str(getattr(mt5_constants, "ORDER_STATE_FILLED", 4)): "FILLED",
                str(getattr(mt5_constants, "ORDER_STATE_CANCELED", 2)): "CANCELLED",
                str(getattr(mt5_constants, "ORDER_STATE_EXPIRED", 6)): "EXPIRED",
                str(getattr(mt5_constants, "ORDER_STATE_REJECTED", 5)): "REJECTED",
                str(getattr(mt5_constants, "ORDER_STATE_PARTIAL", 3)): "PARTIALLY_FILLED_TERMINAL",
            }
            # MT5 history state is a numeric enum; preserve unknown values rather than guessing.
            normalized = state_map.get(state_name, "UNKNOWN")
            if normalized in {"CANCELLED", "EXPIRED", "REJECTED"}:
                try:
                    initial = float(record.get("volume_initial", record.get("volume", 0.0)) or 0.0)
                    remaining = float(record.get("volume_current", record.get("volume", 0.0)) or 0.0)
                except (TypeError, ValueError, OverflowError):
                    initial = remaining = 0.0
                if initial > 0.0 and 0.0 < remaining < initial:
                    # The terminal order retained an unfilled remainder: broker
                    # history therefore proves a partial fill before termination.
                    normalized = "PARTIALLY_FILLED_TERMINAL"
            return {"status": normalized, "order_id": ticket, "broker_confirmed": normalized != "UNKNOWN", "record": record}
        return {"status": "UNKNOWN", "order_id": ticket, "reason": "not_present_in_live_or_history_book", "broker_confirmed": False}

    def execute_entry(
        self,
        symbol: str,
        direction: str,
        lots: float,
        sl: float,
        tp: float,
        comment: str = "",
        stake_usd: Optional[float] = None,
        idempotency_key: str = "",
    ) -> OrderResult:
        """Place a market entry through the executor-only broker path.

        Optional telemetry records confirmed broker outcomes after submission.
        A telemetry failure never changes, retries, or suppresses the broker result.
        """
        connector = self.get_connector(symbol)
        platform = "mt5" if isinstance(connector, MT5Connector) else "deriv"
        monitor = getattr(self, "execution_monitor", None)
        attempt_quote = None
        if monitor is not None:
            try:
                attempt_quote = self.get_price(symbol)
            except Exception as exc:  # noqa: BLE001 — telemetry is non-authoritative
                _pm_logger.debug("[pm] execution telemetry quote unavailable for {}: {}", symbol, exc)

        started_monotonic = _time.monotonic()
        result = None
        try:
            with self._broker_write_lock:
                if isinstance(connector, DerivConnector):
                    result = connector.place_order(
                        symbol, direction, lots, sl, tp, comment,
                        idempotency_key=idempotency_key,
                        stake_usd=stake_usd,
                    )
                else:
                    result = connector.place_order(
                        symbol, direction, lots, sl, tp, comment,
                        idempotency_key=idempotency_key,
                    )
        finally:
            latency_ms = max((_time.monotonic() - started_monotonic) * 1000.0, 0.0)
            if monitor is not None:
                try:
                    pip_size = None
                    try:
                        instrument = get_instrument(symbol)
                        raw_pip_size = getattr(instrument, "pip_size", None)
                        if raw_pip_size is not None:
                            candidate = float(raw_pip_size)
                            if math.isfinite(candidate) and candidate > 0.0:
                                pip_size = candidate
                    except (TypeError, ValueError, OverflowError):
                        pip_size = None

                    spread = None
                    if (
                        attempt_quote is not None
                        and getattr(attempt_quote, "source_timestamp_verified", False) is True
                        and str(getattr(attempt_quote, "quote_kind", "") or "").lower() == "bid_ask"
                        and str(getattr(attempt_quote, "symbol", "") or "").strip().upper()
                            == str(symbol or "").strip().upper()
                    ):
                        try:
                            bid = float(attempt_quote.bid)
                            ask = float(attempt_quote.ask)
                            quote_time = getattr(attempt_quote, "time", None)
                            quote_age = (
                                _time.time() - quote_time.timestamp()
                                if isinstance(quote_time, datetime)
                                and quote_time.tzinfo is not None
                                and quote_time.utcoffset() is not None
                                else float("inf")
                            )
                            if math.isfinite(bid) and math.isfinite(ask) and bid > 0.0 and ask > bid and 0.0 <= quote_age <= 5.0:
                                spread = ask - bid
                        except (TypeError, ValueError, OverflowError, OSError):
                            spread = None

                    monitor.record_order_attempt(
                        symbol=symbol,
                        success=getattr(result, "success", False) is True,
                        latency_ms=latency_ms,
                        requested_price=getattr(result, "requested_price", None),
                        fill_price=getattr(result, "fill_price", None),
                        pip_size=pip_size,
                        spread=spread,
                        observed_epoch=_time.time(),
                        order_type="market",
                        order_id=getattr(result, "order_id", None),
                        lots=getattr(result, "lots", lots),
                        direction=getattr(result, "direction", direction),
                        platform=str(getattr(result, "platform", platform) or platform),
                    )
                except Exception as exc:  # noqa: BLE001 — monitor must never affect execution
                    _pm_logger.warning("[pm] execution telemetry failed for {}: {}", symbol, exc)

        if result.success:
            logger.info(
                "[{}] ENTRY {} {} {:.2f} lots @ {:.5f} | slip {:.1f}pip | {:.0f}ms",
                platform.upper(), direction, symbol, lots,
                result.fill_price, result.slippage_pips, latency_ms,
            )
        else:
            logger.error(
                "[{}] ENTRY FAILED {} {} {:.2f} lots: {}",
                platform.upper(), direction, symbol, lots, result.error,
            )
        return result


    def modify_trade(
        self,
        order_id: str,
        platform: str,
        new_sl: Optional[float] = None,
        new_tp: Optional[float] = None,
    ) -> bool:
        """Modify a position's SL/TP. **Executor-only broker mutator.**

        Intended to be driven by the Execution Division via an ``Intent``.
        """
        # platform string may be "mt5", "mt5_0", "mt5_1", or "deriv"
        connector = self._connector_by_platform_str(platform)
        with self._broker_write_lock:
            return connector.modify_order(order_id, new_sl, new_tp)

    def close_trade(
        self,
        order_id: str,
        platform: str,
        lots: Optional[float] = None,
    ) -> CloseResult:
        """Close (or partially close) a position. **Executor-only broker mutator.**

        Intended to be driven by the Execution Division via an ``Intent``.
        """
        connector = self._connector_by_platform_str(platform)
        with self._broker_write_lock:
            result = connector.close_order(order_id, lots)
        monitor = getattr(self, "execution_monitor", None)
        if monitor is not None:
            try:
                clean_platform = str(platform or "unknown").strip().lower().split("_", 1)[0]
                monitor.record_close_result(
                    order_id=order_id,
                    platform=clean_platform,
                    success=getattr(result, "success", False) is True,
                    requested_price=getattr(result, "requested_price", None),
                    fill_price=getattr(result, "close_price", None),
                    lots_closed=getattr(result, "lots_closed", None),
                    observed_epoch=_time.time(),
                )
            except Exception as exc:  # noqa: BLE001 — monitoring cannot change close result
                _pm_logger.warning("[pm] close telemetry failed for {}: {}", order_id, exc)
        return result

    def get_realized_pnl(self, order_id: str, platform: str) -> Optional[float]:
        connector = self._connector_by_platform_str(platform)
        return connector.get_realized_pnl(order_id)

    def get_deal_close_info(self, order_id: str, platform: str) -> Optional[DealCloseInfo]:
        connector = self._connector_by_platform_str(platform)
        return connector.get_deal_close_info(order_id)

    def get_pending_order_fill_deals(
        self, order_id: str, position_id: str, *, symbol: str,
        platform: str, from_dt: datetime, to_dt: datetime,
    ) -> list[dict]:
        """Return source-verified entry/exit deals for one pending-order position."""
        connector = self._connector_by_platform_str(platform)
        fetch = getattr(connector, "get_deal_history", None)
        if not callable(fetch):
            return []
        canonical_symbol = str(symbol or "").strip().upper()
        ticket = str(order_id or "").strip()
        position_ticket = str(position_id or "").strip()
        rows = fetch(from_dt, to_dt)
        return [
            row for row in (rows or [])
            if isinstance(row, dict)
            and row.get("source_verified") is True
            and str(row.get("symbol", "")).strip().upper() == canonical_symbol
            and str(row.get("position_id", "")).strip() == position_ticket
            and (not ticket or str(row.get("order", "")).strip() == ticket)
        ]

    def get_symbol_spec(self, symbol: str) -> dict:
        """Return broker symbol spec for *symbol*, or ``{}`` when unavailable."""
        try:
            connector = self.get_connector(symbol)
        except Exception as exc:
            logger.debug("[platform_manager] get_symbol_spec connector lookup failed {}: {}", symbol, exc)
            return {}
        try:
            spec_fn = getattr(connector, "get_symbol_spec", None)
            if callable(spec_fn):
                spec = spec_fn(symbol)
                if isinstance(spec, dict):
                    return spec
        except Exception as exc:
            logger.debug("[platform_manager] get_symbol_spec failed {}: {}", symbol, exc)
        return {}

    def _connector_by_platform_str(self, platform: str) -> BaseConnector:
        """Resolve "mt5", "mt5_0", "mt5_1", "deriv" to the right connector."""
        if platform == "deriv":
            return self.deriv
        if platform.startswith("mt5"):
            if "_" in platform and platform != "mt5":
                try:
                    idx = int(platform.split("_")[1])
                    return self.mt5_connectors[idx]
                except (IndexError, ValueError) as exc:
                    logger.debug("[platform_manager] MT5 connector index parse failed: {}", exc)
                    pass
            return self._first_connected_mt5()
        return self.deriv

    # ── Positions ────────────────────────────────────────────────────────

    def get_all_open_positions(self) -> list[PositionInfo]:
        """Return positions only when every currently connected platform responds.

        A partial list is unsafe for duplicate-order checks and portfolio risk.
        Callers that need to distinguish disconnected from confirmed-empty should
        use get_open_positions_snapshot(); this compatibility method raises if
        any connected platform failed to confirm its book.
        """
        snapshot = self.get_open_positions_snapshot()
        if snapshot.failed_platforms:
            raise RuntimeError(
                "open-position book is incomplete; failed platform(s): "
                + ", ".join(sorted(str(item) for item in snapshot.failed_platforms))
            )
        return list(snapshot.positions)

    def get_tick_history(self, symbol: str, from_dt: datetime, to_dt: datetime) -> pd.DataFrame:
        """Fetch historical ticks from the owning connector when supported."""
        connector = self.get_connector(symbol)
        fetch = getattr(connector, "get_ticks", None)
        if not callable(fetch):
            return pd.DataFrame()
        return fetch(symbol, from_dt, to_dt)

    def calculate_broker_profit(
        self, symbol: str, direction: str, volume: float, price_open: float, price_close: float,
    ) -> float:
        """Return broker-calculated P&L in account currency where supported."""
        connector = self.get_connector(symbol)
        calculate = getattr(connector, "calculate_profit", None)
        if not callable(calculate):
            raise NotImplementedError(f"Broker profit calculation is unavailable for {symbol}")
        return float(calculate(symbol, direction, volume, price_open, price_close))

    def calculate_broker_margin(
        self, symbol: str, direction: str, volume: float, price: float,
    ) -> float:
        """Return broker-calculated margin in account currency where supported."""
        connector = self.get_connector(symbol)
        calculate = getattr(connector, "calculate_margin", None)
        if not callable(calculate):
            raise NotImplementedError(f"Broker margin calculation is unavailable for {symbol}")
        return float(calculate(symbol, direction, volume, price))

    def get_mt5_position_snapshots(self) -> list[dict]:
        """Return raw open-position records from each connected MT5 account."""
        snapshots: list[dict] = []
        for index, connector in enumerate(self.mt5_connectors):
            if index >= len(self._mt5_connected_flags) or not self._mt5_connected_flags[index]:
                continue
            try:
                snapshots.extend({
                    **row,
                    "connector_index": index,
                } for row in connector.get_position_snapshots())
            except Exception as exc:
                _pm_logger.warning("[positions] MT5 raw position snapshot failed for connector {}: {}", index, exc)
        return snapshots

    def get_mt5_pending_order_snapshots(self) -> list[dict]:
        """Return raw pending-order records from each connected MT5 account."""
        snapshots: list[dict] = []
        for index, connector in enumerate(self.mt5_connectors):
            if index >= len(self._mt5_connected_flags) or not self._mt5_connected_flags[index]:
                continue
            try:
                snapshots.extend({
                    **row,
                    "connector_index": index,
                } for row in connector.get_pending_order_snapshots())
            except Exception as exc:
                _pm_logger.warning("[orders] MT5 raw pending-order snapshot failed for connector {}: {}", index, exc)
        return snapshots

    def get_mt5_account_snapshots(self) -> list[dict]:
        """Return verified account snapshots from every connected MT5 account."""
        snapshots: list[dict] = []
        for index, connector in enumerate(self.mt5_connectors):
            if index >= len(self._mt5_connected_flags) or not self._mt5_connected_flags[index]:
                continue
            try:
                snapshots.append({
                    "connector_index": index,
                    **connector.get_account_snapshot(),
                })
            except Exception as exc:
                _pm_logger.warning("[account] MT5 account snapshot failed for connector {}: {}", index, exc)
        return snapshots

    def get_mt5_terminal_snapshots(self) -> list[dict]:
        """Return verified terminal health/version snapshots for connected MT5 accounts."""
        snapshots: list[dict] = []
        for index, connector in enumerate(self.mt5_connectors):
            if index >= len(self._mt5_connected_flags) or not self._mt5_connected_flags[index]:
                continue
            try:
                snapshots.append({
                    "connector_index": index,
                    **connector.get_terminal_snapshot(),
                })
            except Exception as exc:
                _pm_logger.warning("[terminal] MT5 terminal snapshot failed for connector {}: {}", index, exc)
        return snapshots

    def get_market_depth(self, symbol: str) -> list[dict]:
        """Best-effort Level-2 order book for ``symbol`` (empty when unsupported).

        Routes to the owning connector and normalizes to a list of
        ``{price, volume, side}`` levels. Fail-safe: any routing/feed fault or a
        broker without DOM yields ``[]`` so the caller's snapshot degrades
        gracefully rather than raising.
        """
        try:
            connector = self.get_connector(symbol)
        except Exception:  # noqa: BLE001 — no platform / not connected
            return []
        if connector is None:
            return []
        try:
            depth = connector.get_market_depth(symbol)
            return list(depth) if depth else []
        except Exception as exc:  # noqa: BLE001
            logger.debug("Market depth fetch failed — {}: {}", symbol, exc)
            return []

    def get_open_positions_snapshot(self) -> BrokerPositionsSnapshot:
        """Fetch broker positions with per-platform confirmation tracking.

        Unlike ``get_all_open_positions``, this method records *which*
        platforms positively answered.  A position may only be considered
        absent (and therefore closed) if its own platform is in
        ``confirmed_platforms``.  If a platform is in ``failed_platforms``,
        its positions are unknown — not absent.

        MT5 is marked confirmed only when **all** connected brokers
        respond.  If any single MT5 broker fails, "mt5" is failed
        (conservative — we cannot distinguish which broker owns which
        position since all carry ``platform="mt5"``).
        """
        snap = BrokerPositionsSnapshot()

        mt5_all_ok = True
        for i, connector in enumerate(self.mt5_connectors):
            if self._mt5_connected_flags[i]:
                try:
                    snap.positions.extend(connector.get_open_positions())
                except Exception as exc:
                    mt5_all_ok = False
                    label = f"MT5[{i}]" if len(self.mt5_connectors) > 1 else "MT5"
                    logger.warning("{} positions fetch error (snapshot): {}", label, exc)

        if any(self._mt5_connected_flags):
            if mt5_all_ok:
                snap.confirmed_platforms.add("mt5")
            else:
                snap.failed_platforms.add("mt5")
        elif any(self._mt5_was_connected):
            snap.failed_platforms.add("mt5")

        if self._deriv_connected:
            try:
                snap.positions.extend(self.deriv.get_open_positions())
                snap.confirmed_platforms.add("deriv")
                if self._deriv_reconnect_warned:
                    logger.info("Deriv positions fetch recovered (snapshot)")
                    self._deriv_reconnect_warned = False
            except Exception as exc:
                snap.failed_platforms.add("deriv")
                if "reconnecting" in str(exc).lower():
                    if not self._deriv_reconnect_warned:
                        logger.warning("Deriv positions fetch blocked (snapshot) — broker is reconnecting")
                        self._deriv_reconnect_warned = True
                else:
                    logger.warning("Deriv positions fetch error (snapshot): {}", exc)
        elif self._deriv_was_connected:
            snap.failed_platforms.add("deriv")

        return snap

    # ── Account ──────────────────────────────────────────────────────────

    def _refresh_deriv_token(self) -> tuple[str, float]:
        """Source a rotated Deriv access token for the connector's proactive refresh.

        Deriv OAuth2 has no refresh-token grant, so a fresh token must be
        supplied out-of-band. ``DERIV_ACCESS_TOKEN_FILE`` (preferred — a
        secret-manager mount that can be updated while the process runs) is read
        first, falling back to the ``DERIV_ACCESS_TOKEN`` env var. Returns
        ``(token, expires_in)``; the connector ignores the result when the
        token is unchanged, so re-reading the same source is a safe no-op.
        """
        token = ""
        token_file = os.getenv("DERIV_ACCESS_TOKEN_FILE", "").strip()
        if token_file:
            try:
                token = Path(token_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                logger.warning("Deriv token file {} unreadable: {}", token_file, exc)
        if not token:
            token = os.getenv("DERIV_ACCESS_TOKEN", "").strip()
        return token, _deriv_token_ttl_from_env()

    def get_account_summary(self) -> dict[str, AccountInfo]:
        summary: dict[str, AccountInfo] = {}
        for i, connector in enumerate(self.mt5_connectors):
            if self._mt5_connected_flags[i]:
                key = f"mt5_{i}" if len(self.mt5_connectors) > 1 else "mt5"
                try:
                    info = connector.get_account_info()
                    summary[key] = info
                    if info is not None and info.balance is not None and info.balance >= 0:
                        self._last_known_balances[key] = float(info.balance)
                except Exception as exc:
                    label = f"MT5[{i}]" if len(self.mt5_connectors) > 1 else "MT5"
                    logger.warning("{} account info error: {}", label, exc)
        if self._deriv_connected:
            try:
                info = self.deriv.get_account_info()
                summary["deriv"] = info
                if info is not None and info.balance is not None and info.balance >= 0:
                    self._last_known_balances["deriv"] = float(info.balance)
                if self._deriv_reconnect_warned:
                    logger.info("Deriv account info recovered")
                    self._deriv_reconnect_warned = False
            except Exception as exc:
                if "reconnecting" in str(exc).lower():
                    if not self._deriv_reconnect_warned:
                        logger.warning("Deriv account info blocked — broker is reconnecting")
                        self._deriv_reconnect_warned = True
                else:
                    logger.warning("Deriv account info error: {}", exc)
        return summary

    def get_balances_by_platform(self) -> dict[str, float]:
        """Per-platform balances, treated independently and never collided.

        Each connected platform reports its live balance; a platform that is
        currently disconnected/erroring falls back to its last-known good
        balance so its capital is still represented. Keys are platform keys
        (e.g. ``mt5``, ``deriv``). MT5 real and Deriv demo balances are kept in
        separate entries and are never summed into one another's slot.
        """
        balances: dict[str, float] = dict(self._last_known_balances)
        for key, info in self.get_account_summary().items():
            try:
                bal = float(info.balance)
            except (TypeError, ValueError):  # SAFE-FALLBACK: omit account with malformed balance from summary
                continue
            if bal >= 0:
                balances[key] = bal
        return balances

    def get_total_balance(self) -> float:
        """Pooled balance across platforms, resilient to transient disconnects.

        Uses each platform's last-known balance when it is momentarily offline
        so the pooled figure stays stable instead of collapsing to a single
        leg's balance during a reconnect.
        """
        return sum(self.get_balances_by_platform().values())

    def get_platform_balance(self, symbol: str) -> float:
        """Get balance for the specific platform that handles this symbol."""
        try:
            connector = self.get_connector(symbol)
        except Exception as exc:
            logger.warning("Balance lookup failed for {}: {}", symbol, exc)
            return 0.0

        platform_name = "mt5" if isinstance(connector, MT5Connector) else "deriv"

        if isinstance(connector, MT5Connector):
            idx = self._connector_index(connector)
            # Same key scheme as get_account_summary so the last-known cache aligns.
            bal_key = (
                f"mt5_{idx}"
                if (idx is not None and len(self.mt5_connectors) > 1)
                else "mt5"
            )
            if idx is not None and self._mt5_connected_flags[idx]:
                for attempt in range(2):
                    try:
                        balance = float(connector.get_account_info().balance)
                        if balance >= 0:
                            self._last_known_balances[bal_key] = balance
                        logger.debug(
                            "Balance for {} → {} platform: ${:.2f}",
                            symbol, platform_name, balance,
                        )
                        return balance
                    except Exception as exc:
                        if attempt == 0:
                            logger.debug(
                                "MT5 balance fetch attempt 1 failed for {}: {} — retrying",
                                symbol,
                                exc,
                            )
                            _time.sleep(1)
                        else:
                            logger.warning(
                                "MT5 balance fetch failed for {} after 2 attempts: {}",
                                symbol,
                                exc,
                            )
            # Bug #17: never return a phantom $0 on a transient error/disconnect —
            # fall back to the last-known good balance so sizing is not corrupted.
            stale = self._last_known_balances.get(bal_key, 0.0)
            if stale > 0:
                logger.warning(
                    "MT5 balance unavailable for {} — using last-known ${:.2f}",
                    symbol, stale,
                )
            return stale

        if isinstance(connector, DerivConnector) and self._deriv_connected:
            for attempt in range(2):
                try:
                    balance = float(self.deriv.get_account_info().balance)
                    if balance >= 0:
                        self._last_known_balances["deriv"] = balance
                    logger.debug(
                        "Balance for {} → {} platform: ${:.2f}",
                        symbol, platform_name, balance,
                    )
                    return balance
                except Exception as exc:
                    if attempt == 0:
                        logger.debug(
                            "Deriv balance fetch attempt 1 failed for {}: {} — retrying",
                            symbol,
                            exc,
                        )
                        _time.sleep(1)
                    else:
                        logger.warning(
                            "Deriv balance fetch failed for {} after 2 attempts: {}",
                            symbol,
                            exc,
                        )

        # Bug #17: Deriv disconnected or fetch failed — return last-known good
        # balance for this platform rather than a phantom $0.
        stale = self._last_known_balances.get(platform_name, 0.0)
        if stale > 0:
            logger.warning(
                "Deriv balance unavailable for {} — using last-known ${:.2f}",
                symbol, stale,
            )
        return stale

    def get_total_equity(self) -> float:
        total = 0.0
        for info in self.get_account_summary().values():
            total += info.equity
        return total

    def _connector_index(self, connector: MT5Connector) -> Optional[int]:
        """Return the list index of a connector, or None if not found."""
        for i, c in enumerate(self.mt5_connectors):
            if c is connector:
                return i
        return None

    # ── Market data ──────────────────────────────────────────────────────

    def fetch_market_data_range(
        self,
        symbol: str,
        timeframes: list[str],
        from_dt: datetime,
        to_dt: datetime,
        *,
        include_forming: bool = False,
    ) -> dict[str, pd.DataFrame]:
        """Fetch historical MT5 bar ranges for replay and hindcast consumers."""
        connector = self.get_connector(symbol)
        fetch = getattr(connector, "get_ohlcv_range", None)
        if not callable(fetch):
            raise NotImplementedError(f"Historical range retrieval is unavailable for {symbol}")
        return {
            timeframe: fetch(
                symbol, timeframe, from_dt, to_dt, include_forming=include_forming,
            )
            for timeframe in timeframes
        }

    def fetch_market_data(
        self,
        symbol: str,
        timeframes: Optional[list[str]] = None,
        count: int = 200,
        *,
        include_forming: bool = True,
    ) -> dict[str, pd.DataFrame]:
        """Fetch OHLCV across multiple timeframes for a single symbol.

        ``include_forming`` defaults to True (broker-native: the last row is the
        still-forming bar). Pass False for callers that require confirmed-closed
        bars only; the cache is bypassed in that case so a closed-only request
        never returns (or populates) a forming-bar-inclusive cache entry.
        """
        if timeframes is None:
            timeframes = ["H4", "H1", "M15", "M5"]
        connector = self.get_connector(symbol)
        data: dict[str, pd.DataFrame] = {}

        if not hasattr(self, "_unavailable_symbols"):
            self._unavailable_symbols: set = set()
        if not hasattr(self, "_failed_timeframes"):
            self._failed_timeframes: set[tuple[str, str]] = set()
        if not hasattr(self, "system_warnings"):
            self.system_warnings: list[dict] = []

        for tf in timeframes:
            try:
                cache = getattr(self, "candle_cache", None)
                # Only the forming-bar-inclusive view is cached. A closed-only
                # request bypasses the cache on both read and write so the two
                # views never cross-contaminate.
                cached = (
                    cache.get(symbol, tf, count)
                    if cache is not None and include_forming else None
                )
                if cached is not None:
                    data[tf] = cached
                    continue
                df = connector.get_ohlcv(
                    symbol, tf, count, include_forming=include_forming,
                )
                data[tf] = df
                if cache is not None and include_forming:
                    cache.put(symbol, tf, count, df)
            except Exception as exc:
                exc_str = str(exc)
                if "not available on broker" in exc_str or "not found" in exc_str.lower():
                    if symbol not in self._unavailable_symbols:
                        msg = (
                            f"Symbol '{symbol}' not available on broker — skipping permanently. "
                            "Remove it from enabled_symbols_override or add a broker mapping."
                        )
                        logger.warning(msg)
                        self._unavailable_symbols.add(symbol)
                        self._add_platform_warning("warning", msg, symbol)
                    break
                elif "reconnecting" in exc_str.lower() or "not connected" in exc_str.lower():
                    logger.debug("Skipping {} {} — broker reconnecting", symbol, tf)
                    break
                elif "No candle data" in exc_str:
                    key = (symbol, tf)
                    if key not in self._failed_timeframes:
                        logger.warning("Data fetch failed — {} {}: {}", symbol, tf, exc)
                        self._failed_timeframes.add(key)
                    else:
                        logger.debug("Data fetch failed — {} {}: {}", symbol, tf, exc)
                else:
                    logger.warning("Data fetch failed — {} {}: {}", symbol, tf, exc)
        return data

    @staticmethod
    def _is_fx_weekend(now_utc) -> bool:
        """FX market is closed from Saturday 00:00 UTC through Sunday 21:59 UTC.
        weekday() >= 5 alone is wrong — it treats Sunday after 22:00 UTC as
        weekend even though FX has already opened for the new week."""
        wd = now_utc.weekday()
        if wd == 5:                          # Saturday — always closed
            return True
        if wd == 6 and now_utc.hour < 22:   # Sunday before 22:00 UTC open
            return True
        return False

    def _add_platform_warning(self, level: str, message: str, symbol: str = "") -> None:
        """Append a system warning to the in-memory feed read by the dashboard."""
        from datetime import timezone
        if not hasattr(self, "system_warnings"):
            self.system_warnings: list[dict] = []
        entry = {
            "level": level,
            "symbol": symbol,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.system_warnings.insert(0, entry)
        if len(self.system_warnings) > 200:
            self.system_warnings = self.system_warnings[:200]

    def fetch_all_market_data(
        self,
        symbols: Optional[list[str]] = None,
        timeframes: Optional[list[str]] = None,
        count: int = 200,
        now_utc=None,
    ) -> dict[str, dict[str, pd.DataFrame]]:
        """Fetch OHLCV for every enabled symbol across timeframes.
        Uses a thread pool so all broker calls run concurrently — cuts
        a 65-symbol scan from ~60s sequential to ~8s parallel.
        """
        import datetime as _dt
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from config import is_always_open

        if symbols is None:
            symbols = self.config.enabled_pairs
        if timeframes is None:
            timeframes = ["H4", "H1", "M15", "M5"]

        if now_utc is None:
            now_utc = _dt.datetime.now(timezone.utc)
        is_weekend = self._is_fx_weekend(now_utc)

        unavailable = getattr(self, "_unavailable_symbols", set())
        active_symbols = [
            s for s in symbols
            if not (is_weekend and not is_always_open(s))
            and s not in unavailable
        ]

        all_data: dict[str, dict[str, pd.DataFrame]] = {}

        def _fetch(symbol: str):
            try:
                data = self.fetch_market_data(symbol, timeframes, count)
                return symbol, data
            except ConnectionError:
                logger.debug("Skipping {} — no platform available", symbol)
                return symbol, None
            except Exception as exc:
                logger.warning("Market data error for {}: {}", symbol, exc)
                return symbol, None

        # Cap workers at 10 — more than enough for 65 symbols without
        # hammering broker connections.
        max_workers = min(10, len(active_symbols)) if active_symbols else 1
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mktdata") as pool:
            futures = {pool.submit(_fetch, s): s for s in active_symbols}
            for future in as_completed(futures):
                symbol, data = future.result()
                if data:
                    all_data[symbol] = data

        return all_data

    def begin_price_snapshot(self) -> None:
        """Open a per-cycle price snapshot for the CURRENT thread.

        Within the snapshot, get_price() fetches each symbol from the broker at
        most once and serves the cached tick thereafter, so all positions in a
        management pass see a single consistent price point. Always pair with
        end_price_snapshot() in a finally block.
        """
        self._price_snapshot.cache = {}

    def end_price_snapshot(self) -> None:
        """Close the current thread's price snapshot (back to live fetches)."""
        self._price_snapshot.cache = None

    def get_price(self, symbol: str) -> TickData:
        snap = getattr(self._price_snapshot, "cache", None)
        if snap is not None:
            key = symbol.upper()
            tick = snap.get(key)
            if tick is None:
                tick = self.get_connector(symbol).get_price(symbol)
                snap[key] = tick
            return tick
        return self.get_connector(symbol).get_price(symbol)

    def get_spread(self, symbol: str) -> float:
        return self.get_connector(symbol).get_spread(symbol)
