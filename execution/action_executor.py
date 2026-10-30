"""APEX TRADER — Action Executor (Phase 6).

Serialized, risk-gated bridge between Intents and broker calls.

The executor guarantees:
1. **Serialization** — one broker call at a time per executor instance.
2. **Risk gating** — every intent is validated before execution.
3. **Retry** — transient failures (``ConnectionError``, timeout) are retried
   with exponential backoff; permanent failures (position gone, invalid
   ticket) are not.
4. **Metrics** — counters and latency tracked for observability.

Thread-safe: a ``threading.Lock`` serializes the broker call itself; retry
backoff sleeps run outside the lock so one retrying intent never stalls others.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from threading import Lock
from typing import Any, Callable, Optional, Protocol

from loguru import logger

from execution.intents import Intent, IntentType
from cognition.identity import IDENTITY_STATEMENT, IDENTITY_VERSION
from execution.risk_gate import GateConfig, RiskGate
from platforms.circuit_breaker import CircuitBreaker


# ── Broker interface (duck-typed to PlatformManager) ─────────────────

class BrokerPort(Protocol):
    """Minimal broker interface the executor needs."""

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
    ) -> Any: ...

    def modify_trade(
        self,
        order_id: str,
        platform: str,
        new_sl: Optional[float] = None,
        new_tp: Optional[float] = None,
    ) -> bool: ...

    def close_trade(
        self,
        order_id: str,
        platform: str,
        lots: Optional[float] = None,
    ) -> Any: ...


# ── Result / Metrics dataclasses ─────────────────────────────────────

@dataclass
class ExecutionResult:
    """Outcome of executing a single intent."""

    intent: Intent
    success: bool
    error: Optional[str] = None
    broker_response: Any = None
    execution_time_ms: float = 0.0
    retried: bool = False
    rejected_by_gate: bool = False
    gate_reason: str = ""


@dataclass
class ExecutorMetrics:
    """Cumulative counters — read via ``get_metrics()``."""

    intents_received: int = 0
    intents_executed: int = 0
    intents_rejected: int = 0
    intents_failed: int = 0
    intents_circuit_open: int = 0
    total_retries: int = 0
    total_execution_time_ms: float = 0.0

    @property
    def avg_execution_time_ms(self) -> float:
        if self.intents_executed == 0:
            return 0.0
        return self.total_execution_time_ms / self.intents_executed


# ── Transient-error detection ────────────────────────────────────────

_TRANSIENT_SUBSTRINGS = (
    "timeout",
    "timed out",
    "connection",
    "reconnect",
    "busy",
    "temporarily",
    "try again",
    "network",
)

# Market-closed is an EXPECTED condition (weekend/holiday FX), not a broker
# fault. It must never count toward the circuit breaker's failure budget, and
# retrying is pointless — the market stays closed. Recognized via the
# ``MARKET_CLOSED:`` tag emitted by the connectors as well as raw substrings.
_MARKET_CLOSED_SUBSTRINGS = (
    "market_closed",
    "market closed",
    "market is closed",
)


def _is_transient(error: BaseException | str) -> bool:
    msg = str(error).lower()
    return any(s in msg for s in _TRANSIENT_SUBSTRINGS)


def _is_market_closed(error: BaseException | str) -> bool:
    msg = str(error).lower()
    return any(s in msg for s in _MARKET_CLOSED_SUBSTRINGS)


# ── Executor ─────────────────────────────────────────────────────────

@dataclass
class ExecutorConfig:
    """Tunables for the action executor."""

    max_retries: int = 2
    retry_base_delay_s: float = 0.5
    circuit_failure_threshold: int = 5
    circuit_cooldown_s: float = 60.0
    gate_config: GateConfig = field(default_factory=GateConfig)


class ActionExecutor:
    """Serialized, risk-gated broker execution.

    Usage::

        executor = ActionExecutor(broker=platform_manager)
        result = executor.execute(intent, open_positions)
    """

    def __init__(
        self,
        broker: BrokerPort,
        config: Optional[ExecutorConfig] = None,
    ) -> None:
        self._broker = broker
        self._cfg = config or ExecutorConfig()
        self._gate = RiskGate(self._cfg.gate_config)
        self._lock = Lock()
        self._metrics = ExecutorMetrics()
        self._metrics_lock = Lock()
        # PARTIAL_CLOSE idempotency: unlike OPEN, partials carry no idempotency
        # key, so a lost-ack retry (broker actually reduced the position but
        # returned a transient error) would close the fraction a second time.
        # Record the monotonic time of the last partial per ticket and skip a
        # repeat within a short window.
        self._last_partial_close: dict[str, float] = {}
        self._partial_close_lock = Lock()
        self._partial_close_dedup_s = 5.0
        # Separate breakers per operation class AND per instrument so a failing
        # CLOSE/manage path on one symbol (e.g. an "Invalid stops" MODIFY_SL
        # loop, or a closed market) cannot trip a breaker that gates manage
        # operations on any other symbol, nor the breaker that gates new OPEN
        # entries. OPEN is a single breaker because the target platform is only
        # resolved by symbol routing inside PlatformManager (not known at intent
        # time). Management breakers are created lazily per (symbol, op-class).
        self._circuit_open = CircuitBreaker(
            name="action_executor.open",
            failure_threshold=self._cfg.circuit_failure_threshold,
            cooldown_seconds=self._cfg.circuit_cooldown_s,
        )
        # Management breakers are created lazily PER (symbol, operation-class) so
        # each instrument is fully isolated — a failing MODIFY_SL storm on one
        # symbol can never open a breaker that blocks CLOSE/MODIFY on another
        # symbol. CLOSE (the de-risking exit) is further isolated from MODIFY/
        # PARTIAL so a stuck stop-move can never starve a risk-reducing close.
        self._circuit_manage: dict[str, CircuitBreaker] = {}
        self._breaker_lock = Lock()
        # Edge-triggered circuit logging: names of breakers whose current OPEN
        # episode has already been announced, so a blocked management loop that
        # retries every ~100ms logs the trip once (WARNING) and then stays quiet
        # until the breaker recovers, instead of flooding the log every cycle.
        self._circuit_announced: set[str] = set()
        self._circuit_log_lock = Lock()

    @staticmethod
    def _payoff_geometry_error(intent: Intent) -> Optional[str]:
        """Validate canonical, activated, evidence-backed geometry at the broker boundary.

        The Brain is the sole market judge. This gate does not derive or replace
        any price level; it only checks that the exact evidence-backed geometry
        survives translation intact and remains ordered around the live entry.
        """
        geometry = intent.payoff_geometry
        if not isinstance(geometry, dict):
            return "market_payoff_geometry_missing"
        if (
            str(geometry.get("identity_version", "") or "") != IDENTITY_VERSION
            or str(geometry.get("identity", "") or "") != IDENTITY_STATEMENT
        ):
            return "market_payoff_geometry_identity_mismatch"
        symbol = str(intent.symbol or "").strip().upper()
        geometry_symbol = str(geometry.get("symbol", "") or "").strip().upper()
        if not symbol or geometry_symbol != symbol:
            return "market_payoff_geometry_symbol_mismatch"
        side = str(intent.direction or "").strip().upper()
        side = "LONG" if side in {"LONG", "BUY"} else ("SHORT" if side in {"SHORT", "SELL"} else "")
        if not side or str(geometry.get("direction", "") or "").upper() != side:
            return "market_payoff_geometry_direction_mismatch"
        if geometry.get("activated") is not True:
            return "market_payoff_geometry_not_activated"
        evidence_ids = geometry.get("evidence_ids")
        if not isinstance(evidence_ids, (list, tuple)) or not any(
            str(item or "").strip() for item in evidence_ids
        ):
            return "market_payoff_geometry_has_no_evidence_provenance"

        def finite_positive(value: Any) -> Optional[float]:
            if isinstance(value, bool):
                return None
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return number if math.isfinite(number) and number > 0.0 else None

        entry = finite_positive(geometry.get("entry_price"))
        stop = finite_positive(geometry.get("stop_price"))
        target = finite_positive(geometry.get("target_price"))
        risk = finite_positive(geometry.get("risk_distance"))
        reward = finite_positive(geometry.get("reward_distance"))
        reward_r = finite_positive(geometry.get("reward_r"))
        source_timestamp = finite_positive(geometry.get("evidence_timestamp_epoch"))
        if source_timestamp is None:
            return "market_payoff_geometry_timestamp_missing"
        now_epoch = time.time()
        timeframe = str(geometry.get("entry_timeframe", "") or "").strip().upper()
        timeframe_seconds = {
            "M1": 60, "M2": 120, "M3": 180, "M5": 300, "M10": 600,
            "M15": 900, "M30": 1800, "H1": 3600, "H2": 7200,
            "H4": 14400, "D1": 86400,
        }.get(timeframe)
        if timeframe_seconds is None:
            return "market_payoff_geometry_timeframe_unknown"
        max_age_seconds = float(timeframe_seconds * 3)
        if source_timestamp > now_epoch + 30 or now_epoch - source_timestamp > max_age_seconds:
            return "market_payoff_geometry_stale"
        live_entry = finite_positive(intent.entry_price)
        intent_stop = finite_positive(intent.new_sl)
        intent_target = finite_positive(intent.new_tp)
        if None in (entry, stop, target, risk, reward, reward_r, live_entry, intent_stop, intent_target):
            return "market_payoff_geometry_contains_invalid_price_or_distance"

        tolerance = max(1e-9, abs(entry) * 1e-8)
        if (
            abs(intent_stop - stop) > tolerance
            or abs(intent_target - target) > tolerance
        ):
            return "market_payoff_geometry_levels_changed_after_brain_decision"
        if side == "LONG":
            if not (stop < entry < target and intent_stop < live_entry < intent_target):
                return "market_payoff_geometry_not_ordered_around_live_entry"
        else:
            if not (target < entry < stop and intent_target < live_entry < intent_stop):
                return "market_payoff_geometry_not_ordered_around_live_entry"

        actual_risk = abs(entry - stop)
        actual_reward = abs(target - entry)
        if (
            abs(actual_risk - risk) > max(tolerance, risk * 1e-6)
            or abs(actual_reward - reward) > max(tolerance, reward * 1e-6)
            or abs((actual_reward / actual_risk) - reward_r) > 1e-5
        ):
            return "market_payoff_geometry_distance_mismatch"
        return None

    @staticmethod
    def _decision_feature_snapshot_error(intent: Intent) -> Optional[str]:
        """Require a complete point-in-time evidence ledger for every new OPEN."""
        snapshot = getattr(intent, "decision_feature_snapshot", None)
        if not isinstance(snapshot, dict):
            return "decision_feature_snapshot_missing"
        if (
            snapshot.get("schema") != "apex.decision_feature_snapshot.v1"
            or snapshot.get("captured_at_decision") is not True
            or snapshot.get("snapshot_complete") is not True
        ):
            return "decision_feature_snapshot_incomplete"
        decision_id = str(getattr(intent, "decision_id", "") or "")
        if (
            not decision_id
            or str(snapshot.get("decision_id") or "") != decision_id
            or not str(snapshot.get("snapshot_id") or "")
            or str(snapshot.get("symbol") or "").strip().upper()
               != str(getattr(intent, "symbol", "") or "").strip().upper()
        ):
            return "decision_feature_snapshot_decision_or_symbol_mismatch"
        ids = snapshot.get("evidence_ids")
        features = snapshot.get("features")
        if not isinstance(ids, list) or not ids or not isinstance(features, list) or not features:
            return "decision_feature_snapshot_has_no_features"
        if not set(str(value) for value in ids if str(value)) <= {
            str(row.get("evidence_id") or "") for row in features if isinstance(row, dict)
        }:
            return "decision_feature_snapshot_evidence_lineage_mismatch"
        try:
            decision_epoch = float(snapshot.get("decision_timestamp_epoch"))
            now_epoch = time.time()
            if not math.isfinite(decision_epoch) or decision_epoch <= 0.0:
                return "decision_feature_snapshot_timestamp_invalid"
            if decision_epoch > now_epoch + 30.0 or now_epoch - decision_epoch > 900.0:
                return "decision_feature_snapshot_stale_or_future"
            if int(snapshot.get("evidence_count", -1)) != len(features):
                return "decision_feature_snapshot_evidence_count_mismatch"
            for row in features:
                if not isinstance(row, dict):
                    return "decision_feature_snapshot_feature_invalid"
                source_epoch = float(row.get("source_timestamp_epoch"))
                if (
                    not math.isfinite(source_epoch)
                    or source_epoch <= 0.0
                    or source_epoch > decision_epoch
                ):
                    return "decision_feature_snapshot_contains_future_or_invalid_feature"
        except (TypeError, ValueError, OverflowError):
            return "decision_feature_snapshot_timestamp_invalid"
        return None

    @staticmethod
    def _is_risk_reducing_without_provenance(
        intent: Intent,
        open_positions: dict[str, dict],
    ) -> bool:
        """Allow missing provenance only for unambiguous de-risking intents.

        CLOSE and PARTIAL_CLOSE reduce exposure by definition. MODIFY_SL is only
        considered protective when the new stop is demonstrably tighter for the
        held side. Ambiguous positions, malformed values, and target changes do
        not receive this exception.
        """
        if intent.intent_type in (IntentType.CLOSE, IntentType.PARTIAL_CLOSE):
            return True
        if intent.intent_type != IntentType.MODIFY_SL or intent.new_sl is None:
            return False
        position = open_positions.get(str(intent.position_ticket or ""), {})
        if not isinstance(position, dict):
            return False
        side = str(position.get("direction", "") or "").upper()
        try:
            old_sl = float(position.get("sl", position.get("stop_loss", 0.0)) or 0.0)
            new_sl = float(intent.new_sl)
        except (TypeError, ValueError, OverflowError):
            return False
        if new_sl <= 0.0:
            return False
        if side in ("LONG", "BUY"):
            return old_sl <= 0.0 or new_sl >= old_sl
        if side in ("SHORT", "SELL"):
            return old_sl <= 0.0 or new_sl <= old_sl
        return False

    def _manage_breaker(self, key: str) -> CircuitBreaker:
        """Lazily create/return the per-(symbol, op-class) management breaker."""
        with self._breaker_lock:
            br = self._circuit_manage.get(key)
            if br is None:
                br = CircuitBreaker(
                    name=f"action_executor.manage.{key or 'unknown'}",
                    failure_threshold=self._cfg.circuit_failure_threshold,
                    cooldown_seconds=self._cfg.circuit_cooldown_s,
                )
                self._circuit_manage[key] = br
            return br

    def _breaker_for(
        self, intent: Intent, open_positions: dict[str, dict],
    ) -> CircuitBreaker:
        """Return the breaker that governs this intent's operation class.

        OPEN (new entries) is isolated from the management/exit path. Management
        breakers are keyed PER INSTRUMENT so one symbol's broker rejections
        (e.g. an "Invalid stops" MODIFY_SL loop) cannot starve management of any
        other symbol — each open position is handled independently. Within a
        symbol, CLOSE is isolated from MODIFY/PARTIAL so a stuck stop-move never
        blocks the risk-reducing exit.
        """
        if intent.intent_type == IntentType.OPEN:
            return self._circuit_open
        symbol = str(getattr(intent, "symbol", "") or "")
        if not symbol:
            symbol = str(
                (open_positions.get(intent.position_ticket, {}) or {}).get("symbol", "")
                or ""
            )
        op = "close" if intent.intent_type == IntentType.CLOSE else "modify"
        return self._manage_breaker(f"{symbol}:{op}" if symbol else op)

    def _broker_entry_constraints_error(self, intent: Intent) -> str | None:
        """Fail closed unless the broker exposes a verified, usable MT5-style spec.

        This last-mile check complements (does not replace) connector validation.
        Contract types without a verified lot/stop/trade-mode specification are
        intentionally rejected here until they have an equivalent adapter-specific
        constraint validator; market context alone is never execution authority.
        """
        spec_fn = getattr(self._broker, "get_symbol_spec", None)
        if not callable(spec_fn):
            return "broker_spec_unavailable: shared executor requires verified broker constraints"
        try:
            spec = spec_fn(intent.symbol)
        except Exception as exc:
            return f"broker_spec_unavailable: specification lookup failed ({type(exc).__name__})"
        if not isinstance(spec, dict) or not spec:
            return "broker_spec_unavailable: no live symbol specification"
        if spec.get("source_verified") is not True or spec.get("specification_verified") is not True:
            return "broker_spec_unverified: source/specification provenance is not verified"

        def number(key: str) -> float | None:
            value = spec.get(key)
            if isinstance(value, bool):
                return None
            try:
                parsed = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return parsed if math.isfinite(parsed) else None

        trade_mode = spec.get("trade_mode")
        if isinstance(trade_mode, str):
            if trade_mode.strip().upper() not in {"FULL", "FULL_ACCESS", "TRADE_MODE_FULL"}:
                return f"broker_trade_mode_rejects_entry: {trade_mode!r}"
        elif number("trade_mode") != 4:
            # MT5 SYMBOL_TRADE_MODE_FULL is enum value 4. Other modes are
            # disabled, long-only, short-only, or close-only.
            return "broker_trade_mode_rejects_entry: full trading is not verified"

        minimum = number("volume_min")
        maximum = number("volume_max")
        step = number("volume_step")
        try:
            raw_lots = getattr(intent, "lots", None)
            lots = float(raw_lots) if raw_lots is not None else None
        except (TypeError, ValueError, OverflowError):
            lots = None
        if (
            minimum is None or maximum is None or step is None
            or minimum <= 0.0 or maximum < minimum or step <= 0.0
        ):
            return "broker_volume_constraints_missing: min/max/step must be verified"
        if lots is None or not math.isfinite(lots) or lots < minimum - 1e-9 or lots > maximum + 1e-9:
            return "broker_volume_constraints_reject_entry: requested lots outside min/max"
        steps = (lots - minimum) / step
        if abs(steps - round(steps)) > 1e-6:
            return "broker_volume_constraints_reject_entry: requested lots violate volume step"

        point = number("point")
        stops_level = number("trade_stops_level")
        if point is None or point <= 0.0 or stops_level is None or stops_level < 0.0:
            return "broker_stop_constraints_missing: point/stops level must be verified"
        try:
            entry = float(getattr(intent, "entry_price", 0.0) or 0.0)
            stop = float(getattr(intent, "new_sl", 0.0) or 0.0)
            target = float(getattr(intent, "new_tp", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            return "broker_stop_constraints_reject_entry: malformed entry/stop/target"
        if not all(math.isfinite(value) and value > 0.0 for value in (entry, stop, target)):
            return "broker_stop_constraints_reject_entry: positive entry/stop/target required"
        direction = str(getattr(intent, "direction", "") or "").upper()
        if direction in {"LONG", "BUY"}:
            if not stop < entry < target:
                return "broker_stop_constraints_reject_entry: invalid LONG geometry"
        elif direction in {"SHORT", "SELL"}:
            if not target < entry < stop:
                return "broker_stop_constraints_reject_entry: invalid SHORT geometry"
        else:
            return "broker_stop_constraints_reject_entry: invalid direction"
        minimum_distance = point * stops_level
        if direction in {"LONG", "BUY"}:
            if entry - stop < minimum_distance or target - entry < minimum_distance:
                return "broker_stop_constraints_reject_entry: stop/target inside broker minimum distance"
        else:
            if stop - entry < minimum_distance or entry - target < minimum_distance:
                return "broker_stop_constraints_reject_entry: stop/target inside broker minimum distance"
        return None

    # ── Public API ───────────────────────────────────────────────────

    def execute(
        self,
        intent: Intent,
        open_positions: dict[str, dict],
        account_drawdown_pct: float = 0.0,
    ) -> ExecutionResult:
        """Execute a single intent, serialized with risk gating.

        Parameters
        ----------
        intent:
            The intent to execute.
        open_positions:
            Live position data for risk gate validation.
            ``{ticket: {"symbol": ..., "direction": ..., "sl": ..., "platform": ...}}``.
        account_drawdown_pct:
            Current account drawdown percentage.
        """
        self._inc("intents_received")

        # The canonical identity is versioned and carried with Brain-originated
        # intent provenance. Reject a malformed/mismatched OPEN before it can
        # reach any broker path; risk-reducing management intents remain usable.
        if intent.intent_type == IntentType.OPEN:
            if (
                intent.identity_version != IDENTITY_VERSION
                or intent.identity != IDENTITY_STATEMENT
            ):
                reason = (
                    "identity_contract_mismatch: refusing new exposure from a "
                    "non-canonical intent"
                )
                self._inc("intents_rejected")
                logger.error("EXECUTOR REJECTED | OPEN {} | {}", intent.symbol, reason)
                return ExecutionResult(
                    intent=intent, success=False, rejected_by_gate=True,
                    gate_reason=reason, error=reason,
                )
            provenance = {
                "source": str(intent.source or ""),
                "decision_id": str(intent.decision_id or ""),
                "campaign_id": str(intent.campaign_id or ""),
                "opportunity_id": str(intent.opportunity_id or ""),
            }
            missing = [
                name for name, value in provenance.items()
                if not str(value or "").strip()
            ]
            geometry_error = self._payoff_geometry_error(intent)
            if geometry_error:
                reason = (
                    "brain_market_geometry_required: refusing OPEN without intact "
                    f"activated geometry from fresh market evidence ({geometry_error})"
                )
                self._inc("intents_rejected")
                logger.warning("EXECUTOR REJECTED | OPEN {} | {}", intent.symbol, reason)
                return ExecutionResult(
                    intent=intent, success=False, rejected_by_gate=True,
                    gate_reason=reason, error=reason,
                )
            if provenance["source"] != "ai_brain" or missing:
                reason = (
                    "brain_opportunity_provenance_required: every new exposure "
                    "requires source=ai_brain plus decision_id, campaign_id, "
                    "and activated opportunity_id; "
                    f"source={provenance['source']!r}, missing={missing}"
                )
                self._inc("intents_rejected")
                logger.warning("EXECUTOR REJECTED | OPEN {} | {}", intent.symbol, reason)
                return ExecutionResult(
                    intent=intent, success=False, rejected_by_gate=True,
                    gate_reason=reason, error=reason,
                )
            snapshot_error = self._decision_feature_snapshot_error(intent)
            if snapshot_error:
                reason = (
                    "brain_decision_feature_snapshot_required: refusing OPEN without "
                    f"a complete point-in-time evidence snapshot ({snapshot_error})"
                )
                self._inc("intents_rejected")
                logger.warning("EXECUTOR REJECTED | OPEN {} | {}", intent.symbol, reason)
                return ExecutionResult(
                    intent=intent, success=False, rejected_by_gate=True,
                    gate_reason=reason, error=reason,
                )
            broker_constraints_error = self._broker_entry_constraints_error(intent)
            if broker_constraints_error:
                self._inc("intents_rejected")
                logger.warning(
                    "EXECUTOR REJECTED | OPEN {} | {}",
                    intent.symbol, broker_constraints_error,
                )
                return ExecutionResult(
                    intent=intent, success=False, rejected_by_gate=True,
                    gate_reason=broker_constraints_error, error=broker_constraints_error,
                )

        # Provenance is required on modifications and closes as well as new
        # exposure. The sole exception is MISSING provenance on an unambiguous
        # protective action: blocking a close or a tighter stop for missing
        # metadata would increase risk. Such legacy actions are explicitly marked
        # and remain auditable; a non-empty but mismatched version is rejected.
        if intent.intent_type != IntentType.OPEN:
            version_mismatch = (
                intent.identity_version != IDENTITY_VERSION
                or intent.identity != IDENTITY_STATEMENT
            )
            missing_links = [
                name for name, value in (
                    ("decision_id", intent.decision_id),
                    ("campaign_id", intent.campaign_id),
                    ("opportunity_id", intent.opportunity_id),
                )
                if not str(value or "").strip()
            ]
            if version_mismatch:
                reason = (
                    "identity_contract_mismatch: management intent carries a "
                    "non-canonical identity/version; refusing execution"
                )
            elif missing_links:
                if self._is_risk_reducing_without_provenance(intent, open_positions):
                    logger.warning(
                        "EXECUTOR | legacy_unattributed protective intent | "
                        "type={} symbol={} ticket={} missing={}",
                        intent.intent_type.name, intent.symbol,
                        intent.position_ticket, missing_links,
                    )
                    intent = replace(intent, source="legacy_unattributed")
                else:
                    reason = (
                        "management_provenance_required: intent lacks "
                        f"{missing_links}; only a protective CLOSE/PARTIAL_CLOSE "
                        "or demonstrably tighter stop may proceed without links"
                    )
                    self._inc("intents_rejected")
                    logger.error(
                        "EXECUTOR REJECTED | {} {} | {}",
                        intent.intent_type.name, intent.position_ticket, reason,
                    )
                    return ExecutionResult(
                        intent=intent, success=False, rejected_by_gate=True,
                        gate_reason=reason, error=reason,
                    )
            if version_mismatch:
                self._inc("intents_rejected")
                logger.error(
                    "EXECUTOR REJECTED | {} {} | {}",
                    intent.intent_type.name, intent.position_ticket, reason,
                )
                return ExecutionResult(
                    intent=intent, success=False, rejected_by_gate=True,
                    gate_reason=reason, error=reason,
                )

        # Gold-only is a non-bypassable last-mile check for every new market
        # exposure submitted through this execution plane. Risk-reducing actions
        # on existing positions remain possible, even if an old non-Gold position
        # exists from before the specialist cutover.
        # PARTIAL_CLOSE idempotency guard: skip a repeat partial for the same
        # ticket within the dedup window so a re-submitted (or lost-ack) partial
        # cannot chip the position twice.
        if intent.intent_type == IntentType.PARTIAL_CLOSE:
            tkt = str(intent.position_ticket or "")
            if tkt:
                now_mono = time.monotonic()
                with self._partial_close_lock:
                    last = self._last_partial_close.get(tkt, 0.0)
                    if (now_mono - last) < self._partial_close_dedup_s:
                        logger.warning(
                            "EXECUTOR DEDUP | PARTIAL_CLOSE {} | within {:.0f}s of "
                            "prior partial — skipping to avoid double-close",
                            tkt, self._partial_close_dedup_s,
                        )
                        self._inc("intents_rejected")
                        return ExecutionResult(
                            intent=intent, success=False,
                            error="partial_close_deduped",
                        )
                    self._last_partial_close[tkt] = now_mono

        gate = self._gate.validate(intent, open_positions, account_drawdown_pct)
        if not gate.allowed:
            self._inc("intents_rejected")
            logger.info(
                "EXECUTOR REJECTED | {} {} | {}",
                intent.intent_type.name, intent.position_ticket, gate.reason,
            )
            return ExecutionResult(
                intent=intent,
                success=False,
                rejected_by_gate=True,
                gate_reason=gate.reason,
            )

        breaker = self._breaker_for(intent, open_positions)
        if not breaker.can_execute():
            self._inc("intents_circuit_open")
            status = breaker.get_status()
            reason = (
                f"Circuit open — {status.failure_count} failures, "
                f"cooldown {status.cooldown_remaining_seconds:.0f}s remaining"
            )
            # Edge-triggered: announce the trip once, then stay quiet at DEBUG
            # while the breaker keeps rejecting (the manage loop re-issues the
            # same intent every ~100ms). The recovery is logged in
            # _execute_with_retry when the breaker next closes.
            with self._circuit_log_lock:
                first = breaker.name not in self._circuit_announced
                self._circuit_announced.add(breaker.name)
            if first:
                logger.warning(
                    "EXECUTOR CIRCUIT OPEN | {} {} | {}",
                    intent.intent_type.name, intent.position_ticket, reason,
                )
            else:
                logger.debug(
                    "EXECUTOR CIRCUIT OPEN | {} {} | {}",
                    intent.intent_type.name, intent.position_ticket, reason,
                )
            return ExecutionResult(
                intent=intent, success=False, error=reason,
            )

        return self._execute_with_retry(intent, open_positions)

    def execute_batch(
        self,
        intents: list[Intent],
        open_positions: dict[str, dict],
        account_drawdown_pct: float = 0.0,
    ) -> list[ExecutionResult]:
        """Execute a batch of intents in priority order (highest first)."""
        sorted_intents = sorted(
            intents, key=lambda i: i.priority, reverse=True,
        )
        results: list[ExecutionResult] = []
        for intent in sorted_intents:
            result = self.execute(intent, open_positions, account_drawdown_pct)
            results.append(result)
            if result.success and intent.intent_type == IntentType.CLOSE:
                open_positions.pop(intent.position_ticket, None)
        return results

    def get_metrics(self) -> ExecutorMetrics:
        """Return a snapshot of cumulative metrics."""
        with self._metrics_lock:
            return ExecutorMetrics(
                intents_received=self._metrics.intents_received,
                intents_executed=self._metrics.intents_executed,
                intents_rejected=self._metrics.intents_rejected,
                intents_failed=self._metrics.intents_failed,
                intents_circuit_open=self._metrics.intents_circuit_open,
                total_retries=self._metrics.total_retries,
                total_execution_time_ms=self._metrics.total_execution_time_ms,
            )

    # ── Internal ─────────────────────────────────────────────────────

    def _execute_with_retry(
        self, intent: Intent, open_positions: dict[str, dict],
    ) -> ExecutionResult:
        last_error: Optional[str] = None
        retried = False
        circuit = self._breaker_for(intent, open_positions)

        for attempt in range(1 + self._cfg.max_retries):
            if attempt > 0:
                retried = True
                self._inc("total_retries")
                delay = self._cfg.retry_base_delay_s * (2 ** (attempt - 1))
                time.sleep(delay)

            t0 = time.monotonic()
            try:
                # Serialize only the actual broker call. The retry backoff
                # ``time.sleep`` above runs OUTSIDE the lock so a retrying intent
                # never stalls other broker operations.
                with self._lock:
                    result = self._dispatch(intent, open_positions)
                elapsed_ms = (time.monotonic() - t0) * 1000

                if result.success:
                    circuit.record_success()
                    # Announce recovery once, matching the edge-triggered OPEN
                    # log, so the operator sees the breaker rejoin after it had
                    # tripped — then clear the announce flag for the next episode.
                    with self._circuit_log_lock:
                        recovered = circuit.name in self._circuit_announced
                        self._circuit_announced.discard(circuit.name)
                    if recovered:
                        logger.info(
                            "EXECUTOR CIRCUIT RECOVERED | {} {} | breaker {} closed",
                            intent.intent_type.name, intent.position_ticket,
                            circuit.name,
                        )
                    self._inc("intents_executed")
                    self._add_latency(elapsed_ms)
                    logger.info(
                        "EXECUTOR OK | {} {} | {:.1f}ms{}",
                        intent.intent_type.name,
                        intent.position_ticket,
                        elapsed_ms,
                        " (retried)" if retried else "",
                    )
                    result.execution_time_ms = elapsed_ms
                    result.retried = retried
                    return result

                # Market-closed is expected, not a fault: do NOT record a
                # breaker failure (would otherwise open the manage breaker and
                # block legitimate closes/modifies on 24/7 instruments) and do
                # NOT retry (the market will not reopen within the backoff).
                if _is_market_closed(result.error or ""):
                    self._add_latency(elapsed_ms)
                    logger.info(
                        "EXECUTOR SKIP (market closed) | {} {} | {}",
                        intent.intent_type.name,
                        intent.position_ticket,
                        result.error,
                    )
                    result.execution_time_ms = elapsed_ms
                    result.retried = retried
                    return result

                if not _is_transient(result.error or ""):
                    circuit.record_failure()
                    self._inc("intents_failed")
                    self._add_latency(elapsed_ms)
                    logger.warning(
                        "EXECUTOR FAIL (permanent) | {} {} | {}",
                        intent.intent_type.name,
                        intent.position_ticket,
                        result.error,
                    )
                    result.execution_time_ms = elapsed_ms
                    result.retried = retried
                    return result

                # A transient PARTIAL_CLOSE failure is ambiguous: the broker may
                # have already reduced the position before the ack was lost.
                # Retrying would double-close, so treat it as terminal here
                # rather than re-dispatch (it has no idempotency key like OPEN).
                if intent.intent_type == IntentType.PARTIAL_CLOSE:
                    circuit.record_failure()
                    self._inc("intents_failed")
                    self._add_latency(elapsed_ms)
                    logger.warning(
                        "EXECUTOR FAIL (no-retry partial) | PARTIAL_CLOSE {} | {}",
                        intent.position_ticket, result.error,
                    )
                    result.execution_time_ms = elapsed_ms
                    result.retried = retried
                    return result

                last_error = result.error

            except ConnectionError as exc:
                elapsed_ms = (time.monotonic() - t0) * 1000
                last_error = str(exc)
                if not _is_transient(exc):
                    circuit.record_failure()
                    self._inc("intents_failed")
                    self._add_latency(elapsed_ms)
                    return ExecutionResult(
                        intent=intent, success=False, error=last_error,
                        execution_time_ms=elapsed_ms, retried=retried,
                    )

            except Exception as exc:
                elapsed_ms = (time.monotonic() - t0) * 1000
                last_error = f"{type(exc).__name__}: {exc}"
                circuit.record_failure()
                self._inc("intents_failed")
                self._add_latency(elapsed_ms)
                logger.error(
                    "EXECUTOR ERROR | {} {} | {}",
                    intent.intent_type.name,
                    intent.position_ticket,
                    last_error,
                )
                return ExecutionResult(
                    intent=intent, success=False, error=last_error,
                    execution_time_ms=elapsed_ms, retried=retried,
                )

        circuit.record_failure()
        self._inc("intents_failed")
        logger.warning(
            "EXECUTOR FAIL (retries exhausted) | {} {} | {}",
            intent.intent_type.name, intent.position_ticket, last_error,
        )
        return ExecutionResult(
            intent=intent, success=False, error=last_error, retried=retried,
        )

    def _dispatch(
        self, intent: Intent, open_positions: dict[str, dict],
    ) -> ExecutionResult:
        """Route an intent to the appropriate broker call."""
        pos = open_positions.get(intent.position_ticket, {})
        platform = pos.get("platform", "")

        if intent.intent_type == IntentType.OPEN:
            resp = self._broker.execute_entry(
                symbol=intent.symbol,
                direction=str(intent.direction or ""),
                lots=float(intent.lots or 0.0),
                sl=float(intent.new_sl or 0.0),
                tp=float(intent.new_tp or 0.0),
                comment=str(intent.comment or ""),
                stake_usd=(
                    float(intent.stake_usd)
                    if intent.stake_usd is not None
                    else None
                ),
                idempotency_key=str(intent.idempotency_key or ""),
            )
            success = getattr(resp, "success", bool(resp))
            error = getattr(resp, "error", None) or (None if success else "open failed")
            return ExecutionResult(
                intent=intent, success=success, error=error,
                broker_response=resp,
            )

        if intent.intent_type == IntentType.CLOSE:
            resp = self._broker.close_trade(
                order_id=intent.position_ticket,
                platform=platform,
            )
            success = getattr(resp, "success", bool(resp))
            error = getattr(resp, "error", None) or (None if success else "close failed")
            return ExecutionResult(
                intent=intent, success=success, error=error,
                broker_response=resp,
            )

        if intent.intent_type == IntentType.PARTIAL_CLOSE:
            # Deriv close_order is full-close only — routing a partial there
            # would over-close the entire multiplier contract. Skip rather than
            # silently liquidate the whole position.
            if platform == "deriv":
                logger.warning(
                    "PARTIAL_CLOSE skipped on deriv (full-close only connector) | {}",
                    intent.position_ticket,
                )
                return ExecutionResult(
                    intent=intent, success=False,
                    error="partial close unsupported on deriv (would over-close)",
                )
            remaining = pos.get("remaining_lots", pos.get("lots", 0.0))
            fraction = intent.close_fraction or 0.5
            close_lots = remaining * fraction
            # Snap to the broker volume step so the partial is not rejected for
            # an unaligned volume (hard-coded 2dp rounding could violate step).
            vol_step = pos.get("volume_step", 0.01) or 0.01
            close_lots = round(round(close_lots / vol_step) * vol_step, 8)
            if close_lots <= 0:
                # Position is at (or below) the broker minimum lot — it cannot be
                # split. Reporting a hard failure here would roll back the
                # optimistic partial_closed / tp1_hit flags (see
                # _handle_manage_result, which only rolls back on failure), so the
                # impossible PARTIAL_CLOSE would re-fire every eval cycle forever.
                # Instead report success with a skip marker: the flags stick, the
                # system stops retrying the split it can't do, and the position
                # falls through to SL-based protection (breakeven / trailing) that
                # the profit-protection ladder already provides.
                logger.warning(
                    "PARTIAL_CLOSE min-lot skip: {} lots at minimum "
                    "({} × {}) — using SL protection instead | {}",
                    remaining, remaining, fraction, intent.position_ticket,
                )
                return ExecutionResult(
                    intent=intent, success=True,
                    error="min_lot_skip: position at minimum lot, partial close skipped",
                )
            resp = self._broker.close_trade(
                order_id=intent.position_ticket,
                platform=platform,
                lots=close_lots,
            )
            success = getattr(resp, "success", bool(resp))
            error = getattr(resp, "error", None) or (None if success else "partial close failed")
            return ExecutionResult(
                intent=intent, success=success, error=error,
                broker_response=resp,
            )

        if intent.intent_type == IntentType.MODIFY_SL:
            ok = self._broker.modify_trade(
                order_id=intent.position_ticket,
                platform=platform,
                new_sl=intent.new_sl,
            )
            return ExecutionResult(
                intent=intent, success=bool(ok),
                error=None if ok else "modify_trade returned False",
                broker_response=ok,
            )

        if intent.intent_type == IntentType.MODIFY_TP:
            ok = self._broker.modify_trade(
                order_id=intent.position_ticket,
                platform=platform,
                new_tp=intent.new_tp,
            )
            return ExecutionResult(
                intent=intent, success=bool(ok),
                error=None if ok else "modify_trade returned False",
                broker_response=ok,
            )

        return ExecutionResult(
            intent=intent, success=False,
            error=f"Unknown intent type: {intent.intent_type}",
        )

    # ── Metrics helpers ──────────────────────────────────────────────

    def _inc(self, field: str, n: int = 1) -> None:
        with self._metrics_lock:
            current = getattr(self._metrics, field, 0)
            setattr(self._metrics, field, current + n)

    def _add_latency(self, ms: float) -> None:
        with self._metrics_lock:
            self._metrics.total_execution_time_ms += ms
