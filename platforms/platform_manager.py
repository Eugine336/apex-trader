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
import os
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from loguru import logger as _pm_logger

import pandas as pd

logger = _pm_logger

from config import (
    AppConfig,
    Platform,
    get_instrument,
)
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
            token_expires_in=float(os.getenv("DERIV_TOKEN_EXPIRES_IN", "3600")),
            app_id=os.getenv("DERIV_APP_ID", ""),
            max_tick_age_seconds=self.config.risk.max_tick_age_seconds,
            reconnect_max_attempts=getattr(self.config.ops, "reconnect_max_retries", 10),
            reconnect_base_delay=getattr(self.config.ops, "reconnect_base_delay_seconds", 5.0),
        )

        self._mt5_connected_flags: list[bool] = [False] * len(self.mt5_connectors)
        self._deriv_connected = False

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
                except Exception:
                    pass
        if self._deriv_connected:
            try:
                cid = self.deriv._find_contract_by_idem_key(idem_key)
                if cid:
                    return cid
            except Exception:
                pass
        return None

    def execute_entry(
        self,
        symbol: str,
        direction: str,
        lots: float,
        sl: float,
        tp: float,
        comment: str = "",
        stake_usd: Optional[float] = None,  # Deriv only — risk amount in USD
        idempotency_key: str = "",
    ) -> OrderResult:
        connector = self.get_connector(symbol)
        platform = "mt5" if isinstance(connector, MT5Connector) else "deriv"

        t0 = _time.monotonic()
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
        latency_ms = (_time.monotonic() - t0) * 1000

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

    def place_pending_entry(
        self,
        symbol: str,
        order_kind: str,
        entry_price: float,
        lots: float,
        sl: float,
        tp: float,
        comment: str = "",
        idempotency_key: str = "",
    ) -> OrderResult:
        connector = self.get_connector(symbol)
        platform = "mt5" if isinstance(connector, MT5Connector) else "deriv"
        with self._broker_write_lock:
            result = connector.place_pending_order(
                symbol, order_kind, entry_price, lots, sl, tp, comment,
                idempotency_key=idempotency_key,
            )
        if result.success:
            logger.info(
                "[{}] PENDING {} {} {:.2f} lots @ {:.5f}",
                platform.upper(), order_kind, symbol, lots, entry_price,
            )
        else:
            logger.error(
                "[{}] PENDING FAILED {} {} {:.2f} lots: {}",
                platform.upper(), order_kind, symbol, lots, result.error,
            )
        return result

    def modify_trade(
        self,
        order_id: str,
        platform: str,
        new_sl: Optional[float] = None,
        new_tp: Optional[float] = None,
    ) -> bool:
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
        connector = self._connector_by_platform_str(platform)
        with self._broker_write_lock:
            return connector.close_order(order_id, lots)

    def get_realized_pnl(self, order_id: str, platform: str) -> Optional[float]:
        connector = self._connector_by_platform_str(platform)
        return connector.get_realized_pnl(order_id)

    def get_deal_close_info(self, order_id: str, platform: str) -> Optional[DealCloseInfo]:
        connector = self._connector_by_platform_str(platform)
        return connector.get_deal_close_info(order_id)

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
        positions: list[PositionInfo] = []
        for i, connector in enumerate(self.mt5_connectors):
            if self._mt5_connected_flags[i]:
                try:
                    positions.extend(connector.get_open_positions())
                except Exception as exc:
                    label = f"MT5[{i}]" if len(self.mt5_connectors) > 1 else "MT5"
                    logger.warning("{} positions fetch error: {}", label, exc)
        if self._deriv_connected:
            try:
                positions.extend(self.deriv.get_open_positions())
                if self._deriv_reconnect_warned:
                    logger.info("Deriv positions fetch recovered")
                    self._deriv_reconnect_warned = False
            except Exception as exc:
                if "reconnecting" in str(exc).lower():
                    if not self._deriv_reconnect_warned:
                        logger.warning("Deriv positions fetch blocked — broker is reconnecting")
                        self._deriv_reconnect_warned = True
                else:
                    logger.warning("Deriv positions fetch error: {}", exc)
        return positions

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

    def get_account_summary(self) -> dict[str, AccountInfo]:
        summary: dict[str, AccountInfo] = {}
        for i, connector in enumerate(self.mt5_connectors):
            if self._mt5_connected_flags[i]:
                key = f"mt5_{i}" if len(self.mt5_connectors) > 1 else "mt5"
                try:
                    summary[key] = connector.get_account_info()
                except Exception as exc:
                    label = f"MT5[{i}]" if len(self.mt5_connectors) > 1 else "MT5"
                    logger.warning("{} account info error: {}", label, exc)
        if self._deriv_connected:
            try:
                summary["deriv"] = self.deriv.get_account_info()
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

    def get_total_balance(self) -> float:
        total = 0.0
        for info in self.get_account_summary().values():
            total += info.balance
        return total

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
            if idx is not None and self._mt5_connected_flags[idx]:
                for attempt in range(2):
                    try:
                        balance = float(connector.get_account_info().balance)
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
            return 0.0

        if isinstance(connector, DerivConnector) and self._deriv_connected:
            for attempt in range(2):
                try:
                    balance = float(self.deriv.get_account_info().balance)
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

        return 0.0

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

    def fetch_market_data(
        self,
        symbol: str,
        timeframes: Optional[list[str]] = None,
        count: int = 200,
    ) -> dict[str, pd.DataFrame]:
        """Fetch OHLCV across multiple timeframes for a single symbol."""
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
                cached = cache.get(symbol, tf, count) if cache is not None else None
                if cached is not None:
                    data[tf] = cached
                    continue
                df = connector.get_ohlcv(symbol, tf, count)
                data[tf] = df
                if cache is not None:
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
