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
    # Hard final-boundary gate for Apex's Gold-specialist deployment. Defaults
    # off for generic/research consumers; EventDrivenSystem explicitly enables it.
    gold_only: bool = False
    gold_symbol_aliases: tuple[str, ...] = (
        "XAUUSD", "GOLD", "XAUUSDM", "XAUUSD.M", "XAUUSD.PRO", "XAUUSD.A",
    )
    # Gold-specialist runtime can require current broker-backed OHLC/quote data.
    # Generic executor consumers leave this off unless they supply a health callback.
    require_gold_feed_health: bool = False
    gold_feed_health_check: Optional[Callable[[str], bool]] = None


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
        # Exact alias allowlist, never prefix matching. Original broker symbol
        # is preserved for routing; canonicalisation is used only for admission.
        self._gold_aliases = {
            str(symbol).strip().upper()
            for symbol in self._cfg.gold_symbol_aliases
            if str(symbol).strip()
        }
        self._gold_aliases.add("XAUUSD")
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
        if self._cfg.gold_only and intent.intent_type == IntentType.OPEN:
            symbol = str(intent.symbol or "").strip().upper()
            if symbol not in self._gold_aliases:
                reason = (
                    f"gold_only_invariant: rejected new exposure for {intent.symbol!r}; "
                    "only explicitly allowlisted XAUUSD aliases are accepted"
                )
                self._inc("intents_rejected")
                logger.warning("EXECUTOR REJECTED | OPEN {} | {}", intent.symbol, reason)
                return ExecutionResult(
                    intent=intent,
                    success=False,
                    rejected_by_gate=True,
                    gate_reason=reason,
                    error=reason,
                )

            if self._cfg.require_gold_feed_health:
                health_check = self._cfg.gold_feed_health_check
                try:
                    feed_healthy = bool(health_check(intent.symbol)) if callable(health_check) else False
                except Exception as exc:  # noqa: BLE001 — feed health must fail closed
                    logger.warning("[gold-only] feed health check failed for {}: {}", intent.symbol, exc)
                    feed_healthy = False
                if not feed_healthy:
                    reason = (
                        f"gold_feed_unhealthy: rejected new exposure for {intent.symbol!r}; "
                        "a fresh, valid broker quote and completed M5 bars are required"
                    )
                    self._inc("intents_rejected")
                    logger.warning("EXECUTOR REJECTED | OPEN {} | {}", intent.symbol, reason)
                    return ExecutionResult(
                        intent=intent,
                        success=False,
                        rejected_by_gate=True,
                        gate_reason=reason,
                        error=reason,
                    )

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
