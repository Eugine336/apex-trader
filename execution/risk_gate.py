"""APEX TRADER — Risk Gate (Phase 6).

Pre-execution validation layer between the Intent Aggregator (Phase 5) and
the Action Executor.  Validates each intent against the current position set
and account state before the executor touches the broker.

The risk gate does NOT replace the RiskEngine (``risk/risk_engine.py``).
RiskEngine governs *entry* decisions; the risk gate governs *execution*
decisions — verifying that the action is still safe to send given what may
have changed since the intent was generated.

Thread-safe: the gate holds no mutable state of its own.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Optional

from execution.intents import Intent, IntentType


@dataclass
class GateConfig:
    """Tunables for the risk gate."""

    max_calls_per_second: float = 5.0
    emergency_drawdown_pct: float = 20.0
    max_sl_loosen_pips: float = 0.0


@dataclass(frozen=True)
class GateResult:
    """Outcome of a risk gate check."""

    allowed: bool
    reason: str = ""


class RiskGate:
    """Pre-execution risk validation.

    Every intent passes through ``validate()`` before the executor sends
    it to the broker.  Checks are intentionally lightweight — the gate
    must never become a bottleneck.
    """

    def __init__(self, config: Optional[GateConfig] = None) -> None:
        self._cfg = config or GateConfig()
        self._rate_lock = Lock()
        self._call_times: list[float] = []

    def validate(
        self,
        intent: Intent,
        open_positions: dict[str, dict],
        account_drawdown_pct: float = 0.0,
    ) -> GateResult:
        """Validate an intent.  Returns ``GateResult(allowed=True)`` on pass.

        Parameters
        ----------
        intent:
            The intent to validate.
        open_positions:
            Mapping ``{ticket: {"symbol": str, "direction": str, "sl": float,
            "platform": str, ...}}``.  Built from the live position store.
        account_drawdown_pct:
            Current account drawdown as a positive percentage (e.g. 8.5 means
            the account is 8.5 % below peak equity).
        """
        result = self._check_position_exists(intent, open_positions)
        if not result.allowed:
            return result

        result = self._check_sl_direction(intent, open_positions)
        if not result.allowed:
            return result

        result = self._check_exposure(intent, account_drawdown_pct)
        if not result.allowed:
            return result

        result = self._check_rate_limit()
        if not result.allowed:
            return result

        return GateResult(allowed=True)

    # ── Individual checks ────────────────────────────────────────────

    def _check_position_exists(
        self, intent: Intent, positions: dict[str, dict],
    ) -> GateResult:
        if intent.position_ticket not in positions:
            return GateResult(
                allowed=False,
                reason=f"Position {intent.position_ticket} no longer open",
            )
        return GateResult(allowed=True)

    def _check_sl_direction(
        self, intent: Intent, positions: dict[str, dict],
    ) -> GateResult:
        if intent.intent_type != IntentType.MODIFY_SL:
            return GateResult(allowed=True)
        if intent.new_sl is None:
            return GateResult(allowed=False, reason="MODIFY_SL intent has no new_sl")

        pos = positions.get(intent.position_ticket, {})
        direction = pos.get("direction", "").upper()
        current_sl = pos.get("sl", 0.0)

        if current_sl <= 0:
            return GateResult(allowed=True)

        if direction in ("BUY", "LONG"):
            if intent.new_sl < current_sl - self._cfg.max_sl_loosen_pips:
                return GateResult(
                    allowed=False,
                    reason=(
                        f"SL loosening blocked for LONG: "
                        f"{current_sl:.5f} → {intent.new_sl:.5f}"
                    ),
                )
        elif direction in ("SELL", "SHORT"):
            if intent.new_sl > current_sl + self._cfg.max_sl_loosen_pips:
                return GateResult(
                    allowed=False,
                    reason=(
                        f"SL loosening blocked for SHORT: "
                        f"{current_sl:.5f} → {intent.new_sl:.5f}"
                    ),
                )

        return GateResult(allowed=True)

    def _check_exposure(
        self, intent: Intent, drawdown_pct: float,
    ) -> GateResult:
        if drawdown_pct >= self._cfg.emergency_drawdown_pct:
            if intent.intent_type in (IntentType.CLOSE, IntentType.PARTIAL_CLOSE):
                return GateResult(allowed=True)
            return GateResult(
                allowed=False,
                reason=(
                    f"Emergency drawdown ({drawdown_pct:.1f}% >= "
                    f"{self._cfg.emergency_drawdown_pct:.1f}%): "
                    f"only CLOSE/PARTIAL_CLOSE allowed"
                ),
            )
        return GateResult(allowed=True)

    def _check_rate_limit(self) -> GateResult:
        if self._cfg.max_calls_per_second <= 0:
            return GateResult(allowed=True)

        now = time.monotonic()
        window = 1.0

        with self._rate_lock:
            self._call_times = [
                t for t in self._call_times if now - t < window
            ]
            if len(self._call_times) >= self._cfg.max_calls_per_second:
                return GateResult(
                    allowed=False,
                    reason=(
                        f"Rate limit: {len(self._call_times)} calls in last "
                        f"{window}s (max {self._cfg.max_calls_per_second})"
                    ),
                )
            self._call_times.append(now)

        return GateResult(allowed=True)
