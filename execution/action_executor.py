"""APEX TRADER — Action Executor (Phase 6).

Serialized, risk-gated bridge between Intents and broker calls.

The executor guarantees:
1. **Serialization** — one broker call at a time per executor instance.
2. **Risk gating** — every intent is validated before execution.
3. **Retry** — transient failures (``ConnectionError``, timeout) are retried
   with exponential backoff; permanent failures (position gone, invalid
   ticket) are not.
4. **Metrics** — counters and latency tracked for observability.

Thread-safe: uses a ``threading.Lock`` to serialize ``execute()`` calls.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Optional, Protocol

from loguru import logger

from execution.intents import Intent, IntentType
from execution.risk_gate import GateConfig, GateResult, RiskGate
from platforms.circuit_breaker import CircuitBreaker


# ── Broker interface (duck-typed to PlatformManager) ─────────────────

class BrokerPort(Protocol):
    """Minimal broker interface the executor needs."""

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


def _is_transient(error: BaseException | str) -> bool:
    msg = str(error).lower()
    return any(s in msg for s in _TRANSIENT_SUBSTRINGS)


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
        self._circuit = CircuitBreaker(
            name="action_executor",
            failure_threshold=self._cfg.circuit_failure_threshold,
            cooldown_seconds=self._cfg.circuit_cooldown_s,
        )

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

        if not self._circuit.can_execute():
            self._inc("intents_circuit_open")
            status = self._circuit.get_status()
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

        with self._lock:
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

        for attempt in range(1 + self._cfg.max_retries):
            if attempt > 0:
                retried = True
                self._inc("total_retries")
                delay = self._cfg.retry_base_delay_s * (2 ** (attempt - 1))
                time.sleep(delay)

            t0 = time.monotonic()
            try:
                result = self._dispatch(intent, open_positions)
                elapsed_ms = (time.monotonic() - t0) * 1000

                if result.success:
                    self._circuit.record_success()
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

                if not _is_transient(result.error or ""):
                    self._circuit.record_failure()
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

                last_error = result.error

            except ConnectionError as exc:
                elapsed_ms = (time.monotonic() - t0) * 1000
                last_error = str(exc)
                if not _is_transient(exc):
                    self._circuit.record_failure()
                    self._inc("intents_failed")
                    self._add_latency(elapsed_ms)
                    return ExecutionResult(
                        intent=intent, success=False, error=last_error,
                        execution_time_ms=elapsed_ms, retried=retried,
                    )

            except Exception as exc:
                elapsed_ms = (time.monotonic() - t0) * 1000
                last_error = f"{type(exc).__name__}: {exc}"
                self._circuit.record_failure()
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

        self._circuit.record_failure()
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
            remaining = pos.get("remaining_lots", pos.get("lots", 0.0))
            fraction = intent.close_fraction or 0.5
            close_lots = round(remaining * fraction, 2)
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
