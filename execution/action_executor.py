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
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Optional, Protocol

from loguru import logger

from execution.intents import Intent, IntentType
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
        # Separate breakers per operation class AND per platform so a failing
        # CLOSE/manage path on one broker (e.g. a closed FX market on MT5 over
        # the weekend) cannot trip the breaker that gates manage operations on
        # a different broker (24/7 Deriv synthetics), nor the breaker that
        # gates new OPEN entries.  OPEN is a single breaker because the target
        # platform is only resolved by symbol routing inside PlatformManager
        # (not known at intent time).  Manage breakers are keyed by platform;
        # the "" key is the fallback when a position's platform is unknown.
        self._circuit_open = CircuitBreaker(
            name="action_executor.open",
            failure_threshold=self._cfg.circuit_failure_threshold,
            cooldown_seconds=self._cfg.circuit_cooldown_s,
        )
        self._circuit_manage: dict[str, CircuitBreaker] = {
            platform: CircuitBreaker(
                name=f"action_executor.manage.{platform or 'unknown'}",
                failure_threshold=self._cfg.circuit_failure_threshold,
                cooldown_seconds=self._cfg.circuit_cooldown_s,
            )
            for platform in ("mt5", "deriv", "")
        }

    def _breaker_for(
        self, intent: Intent, open_positions: dict[str, dict],
    ) -> CircuitBreaker:
        """Return the breaker that governs this intent's operation class.

        OPEN (new entries) is isolated from the management/exit path
        (CLOSE, PARTIAL_CLOSE, MODIFY_*) so failures on one cannot starve
        the other.  Manage operations are further isolated per platform so a
        broker outage / closed market on MT5 cannot block manage operations on
        Deriv (and vice versa).
        """
        if intent.intent_type == IntentType.OPEN:
            return self._circuit_open
        platform = str(
            (open_positions.get(intent.position_ticket, {}) or {}).get(
                "platform", ""
            )
            or ""
        )
        return self._circuit_manage.get(platform, self._circuit_manage[""])

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

        if not self._breaker_for(intent, open_positions).can_execute():
            self._inc("intents_circuit_open")
            status = self._breaker_for(intent, open_positions).get_status()
            reason = (
                f"Circuit open — {status.failure_count} failures, "
                f"cooldown {status.cooldown_remaining_seconds:.0f}s remaining"
            )
            logger.warning(
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
                return ExecutionResult(
                    intent=intent, success=False,
                    error=f"Computed close lots <= 0 ({remaining} × {fraction})",
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
