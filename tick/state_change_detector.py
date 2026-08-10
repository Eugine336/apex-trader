"""APEX TRADER — TickStateChangeDetector (Phase 2 → cognition bridge).

Closes the gap between live tick ingestion and cognitive reasoning.  Ticks
already reach :class:`~tick.tick_router.TickRouter`, which publishes ``"tick"``
events on the :class:`~tick.event_bus.EventBus`, but *no cognitive consumer
subscribed to them*: the Brain woke only on the periodic cycle and on
forming-bar shifts.  This detector sits between the EventBus and the
:class:`~cognition.loop.CognitionLoop` and wakes the Brain the instant the
market *meaningfully* changes for a symbol.

What it does
------------
* Subscribes to ``"tick"`` events (``on_tick`` is the EventBus callback).
* Maintains a tiny **per-symbol running state** — reference price, an adaptive
  per-tick volatility estimate (an ATR-like proxy derived from the tape),
  a spread EMA, an absolute-velocity EMA and a smoothed momentum sign — updated
  on *every* tick with a handful of float operations (no allocations, no
  buffers), so it is safe to run on the hot tick path.
* Detects a **meaningful state change**: a significant directional move
  (accumulated move since the last wake exceeds a multiple of the volatility
  estimate), a spread spike, a velocity spike, a momentum reversal or — when a
  structural-level source is wired — a key-level breach.
* On a meaningful change it calls the wired
  ``on_change(symbol, magnitude)`` sink (the CognitionLoop's
  :meth:`~cognition.loop.CognitionLoop.maybe_reason_on_change`) to wake the
  Brain on THAT symbol at once.

What it deliberately does NOT do
--------------------------------
* It never wakes the Brain on *every* tick — that would be expensive and
  pointless.  It is **rate-limited per symbol** (``min_interval_seconds``,
  mirroring the loop's ``event_min_interval_seconds``) so a fast feed cannot
  re-wake the same symbol faster than the floor.
* It never passes a *direction* — only a non-directional change *magnitude*
  (Constitution §XXIV / §VII Q25: cognition is not gated on a precomputed
  directional pre-read).  The Brain forms direction itself.
* It never touches the confirmed candle-close analysis pipeline — this is
  purely additive.

Fail-safe and thread-safe throughout: ``on_tick`` is called synchronously on
the EventBus publisher's thread (which may be the MT5 poll thread or the Deriv
WS thread), so all per-symbol state is guarded by a lock, and any fault is
swallowed — a detector error must never break tick routing.
"""

from __future__ import annotations

import threading
import time as _time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from loguru import logger

from tick.models import Tick

# Sink that wakes the Brain: (symbol, change_magnitude) -> Any. Wired to the
# CognitionLoop's ``maybe_reason_on_change``. May be ``None`` (detector still
# tracks state and counts changes, but issues no wake — useful in shadow/tests).
ChangeSink = Callable[[str, float], Any]
# Optional structural-level source: symbol -> iterable of price levels. When a
# tick crosses one of these levels it is treated as a meaningful breach. Off by
# default; must be cheap and fail-safe (it is consulted on the tick path).
LevelSource = Callable[[str], Any]


@dataclass
class _SymbolState:
    """Compact per-symbol running state (all scalars, updated in place)."""

    ref_mid: float = 0.0        # baseline for accumulated-move detection (reset on wake)
    last_mid: float = 0.0       # previous tick mid
    last_epoch: float = 0.0     # previous tick wall-clock epoch (for velocity)
    atr: float = 0.0            # EMA of |Δmid| — adaptive per-tick volatility proxy
    spread_ema: float = 0.0     # EMA of the quoted spread
    abs_vel_ema: float = 0.0    # EMA of |instantaneous velocity| (price/second)
    momentum_ema: float = 0.0   # EMA of signed Δmid (its sign is the momentum lean)
    momentum_sign: int = 0      # last non-zero sign of ``momentum_ema``
    ticks: int = 0              # ticks observed for this symbol (warmup gate)
    last_wake_at: float = 0.0   # monotonic time of the last issued wake (rate limit)


class TickStateChangeDetector:
    """Wakes the Brain when a symbol's live market state changes meaningfully.

    Parameters
    ----------
    on_change : ChangeSink, optional
        Sink invoked as ``on_change(symbol, magnitude)`` on a meaningful change
        — wire it to :meth:`CognitionLoop.maybe_reason_on_change`.  ``None``
        leaves the detector observational (it still tracks state and counts
        changes, but never wakes the Brain).
    level_source : LevelSource, optional
        ``symbol -> iterable[float]`` structural levels; a tick that crosses one
        is a meaningful breach.  Off by default.  Must be cheap and fail-safe.
    config : Any, optional
        Threshold carrier read via ``getattr`` (a ``TickStateChangeConfig`` or
        any duck-typed object).  ``None`` uses the documented defaults.
    clock : Callable[[], float], optional
        Monotonic clock for the per-symbol rate limit (injectable for tests).

    Threshold fields read from ``config`` (with defaults):

    * ``warmup_ticks`` (20) — ticks required before a symbol can trigger, so the
      adaptive estimates stabilise and early noise does not wake the Brain.
    * ``vol_alpha`` (0.05), ``spread_alpha`` (0.05), ``velocity_alpha`` (0.3) —
      EMA smoothing factors for the volatility / spread / velocity estimates.
    * ``price_move_atr_fraction`` (6.0) — a directional move triggers when the
      accumulated move since the last wake exceeds this multiple of the per-tick
      volatility estimate (the ATR-like proxy).
    * ``spread_spike_mult`` (2.0) — spread triggers when it exceeds this multiple
      of its rolling mean.
    * ``velocity_spike_mult`` (3.0) — velocity triggers when |velocity| exceeds
      this multiple of its rolling mean.
    * ``reversal_atr_fraction`` (3.0) — a momentum reversal triggers when the
      smoothed momentum sign flips *and* the flipping move is at least this
      multiple of the volatility estimate (filters sign flip-flop on noise).
    * ``min_interval_seconds`` (8.0) — per-symbol wake floor.
    """

    def __init__(
        self,
        *,
        on_change: Optional[ChangeSink] = None,
        level_source: Optional[LevelSource] = None,
        config: Optional[Any] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._on_change = on_change
        self._level_source = level_source
        self._clock = clock or _time.monotonic

        self._warmup_ticks = max(0, int(getattr(config, "warmup_ticks", 20)))
        self._vol_alpha = _clamp01(getattr(config, "vol_alpha", 0.05))
        self._spread_alpha = _clamp01(getattr(config, "spread_alpha", 0.05))
        self._velocity_alpha = _clamp01(getattr(config, "velocity_alpha", 0.3))
        self._price_move_atr_fraction = max(0.0, float(getattr(config, "price_move_atr_fraction", 6.0)))
        self._spread_spike_mult = max(0.0, float(getattr(config, "spread_spike_mult", 2.0)))
        self._velocity_spike_mult = max(0.0, float(getattr(config, "velocity_spike_mult", 3.0)))
        self._reversal_atr_fraction = max(0.0, float(getattr(config, "reversal_atr_fraction", 3.0)))
        self._min_interval_seconds = max(0.0, float(getattr(config, "min_interval_seconds", 8.0)))

        self._states: dict[str, _SymbolState] = {}
        self._lock = threading.Lock()

        # Observability counters.
        self._ticks_seen: int = 0
        self._changes_detected: int = 0
        self._wakes: int = 0
        self._throttled: int = 0

    # ------------------------------------------------------------------
    # Tick path
    # ------------------------------------------------------------------

    def on_tick(self, tick: Tick) -> None:
        """EventBus ``"tick"`` callback — update state and maybe wake the Brain.

        Runs on the publisher's thread; kept to a handful of float operations
        under a short lock so it never stalls tick routing.  Fully fail-safe.
        """
        try:
            symbol = getattr(tick, "symbol", "") or ""
            if not symbol:
                return
            mid = float(getattr(tick, "mid", 0.0) or 0.0)
            if mid <= 0.0:
                return
            spread = float(getattr(tick, "spread", 0.0) or 0.0)
            if spread < 0.0:
                spread = 0.0
            epoch = float(getattr(tick, "epoch", 0.0) or 0.0)
        except Exception:  # noqa: BLE001 — a malformed tick must never raise here
            return

        symbol, magnitude, reasons = self._observe(symbol, mid, spread, epoch)
        # The sink is invoked OUTSIDE the state lock so any latency in the loop's
        # enqueue/wake never serialises other symbols' tick processing.
        if reasons and self._on_change is not None:
            try:
                self._on_change(symbol, magnitude)
            except Exception as exc:  # noqa: BLE001 — the sink must never break routing
                logger.debug(
                    "[tick-state] change sink error for {}: {} — {}",
                    symbol, type(exc).__name__, exc,
                )

    def _observe(
        self, symbol: str, mid: float, spread: float, epoch: float,
    ) -> "tuple[str, float, list[str]]":
        """Update the symbol's running state and decide whether to wake.

        Returns ``(symbol, magnitude, reasons)`` where ``reasons`` is non-empty
        only when a wake should be issued (a meaningful change passed the
        per-symbol rate-limit floor).  All state mutation happens under the lock.
        """
        prev_mid = 0.0
        with self._lock:
            self._ticks_seen += 1
            st = self._states.get(symbol)
            if st is None:
                # First tick for the symbol — seed the state, never trigger.
                self._states[symbol] = _SymbolState(
                    ref_mid=mid, last_mid=mid, last_epoch=epoch,
                    spread_ema=spread, ticks=1,
                )
                return symbol, 0.0, []

            prev_mid = st.last_mid
            dp = mid - prev_mid
            adp = abs(dp)

            # ── Adaptive estimates (seed on first real sample, then EMA) ──
            st.atr = adp if st.atr <= 0.0 else _ema(st.atr, adp, self._vol_alpha)
            st.spread_ema = (
                spread if st.spread_ema <= 0.0
                else _ema(st.spread_ema, spread, self._spread_alpha)
            )
            dt = epoch - st.last_epoch
            avel = 0.0
            if dt > 0.0:
                avel = adp / dt
                st.abs_vel_ema = (
                    avel if st.abs_vel_ema <= 0.0
                    else _ema(st.abs_vel_ema, avel, self._velocity_alpha)
                )
            prev_sign = st.momentum_sign
            st.momentum_ema = _ema(st.momentum_ema, dp, self._velocity_alpha)
            new_sign = 1 if st.momentum_ema > 0.0 else (-1 if st.momentum_ema < 0.0 else 0)
            if new_sign != 0:
                st.momentum_sign = new_sign

            st.last_mid = mid
            st.last_epoch = epoch
            st.ticks += 1

            # Warmup — let the estimates stabilise before we can trigger.
            if st.ticks < self._warmup_ticks:
                return symbol, 0.0, []

            atr = st.atr
            reasons: list[str] = []
            strength = 0.0

            # ── Trigger 1: significant directional move since the last wake ──
            if atr > 0.0 and self._price_move_atr_fraction > 0.0:
                move = abs(mid - st.ref_mid)
                thr = self._price_move_atr_fraction * atr
                if thr > 0.0 and move >= thr:
                    reasons.append("price_move")
                    strength = max(strength, _score(move, thr))

            # ── Trigger 2: spread spike ──
            if st.spread_ema > 0.0 and self._spread_spike_mult > 0.0:
                thr = self._spread_spike_mult * st.spread_ema
                if thr > 0.0 and spread >= thr:
                    reasons.append("spread_spike")
                    strength = max(strength, _score(spread, thr))

            # ── Trigger 3: velocity spike ──
            if st.abs_vel_ema > 0.0 and self._velocity_spike_mult > 0.0 and avel > 0.0:
                thr = self._velocity_spike_mult * st.abs_vel_ema
                if thr > 0.0 and avel >= thr:
                    reasons.append("velocity_spike")
                    strength = max(strength, _score(avel, thr))

            # ── Trigger 4: momentum reversal (sign flip on a non-trivial move) ──
            if (
                atr > 0.0 and prev_sign != 0 and new_sign != 0
                and new_sign != prev_sign and adp >= self._reversal_atr_fraction * atr
            ):
                reasons.append("reversal")
                strength = max(strength, 0.6)

            # ── Trigger 5: structural key-level breach (optional) ──
            if self._level_source is not None and prev_mid > 0.0:
                if self._crossed_level(symbol, prev_mid, mid):
                    reasons.append("level_breach")
                    strength = max(strength, 0.7)

            if not reasons:
                return symbol, 0.0, []

            # A meaningful change occurred — apply the per-symbol wake floor.
            self._changes_detected += 1
            now = self._clock()
            if (
                self._min_interval_seconds > 0.0
                and (now - st.last_wake_at) < self._min_interval_seconds
            ):
                self._throttled += 1
                return symbol, 0.0, []

            # Commit the wake: stamp the floor and reset the move baseline so the
            # next directional trigger measures fresh movement from here.
            st.last_wake_at = now
            st.ref_mid = mid
            self._wakes += 1
            return symbol, min(1.0, strength), reasons

    def _crossed_level(self, symbol: str, prev_mid: float, mid: float) -> bool:
        """True when the [prev_mid, mid] segment straddles any wired level.

        The level source is consulted here (on the tick path) so it must be
        cheap; any fault is swallowed and read as "no breach".
        """
        try:
            lo, hi = (prev_mid, mid) if prev_mid <= mid else (mid, prev_mid)
            if lo == hi:
                return False
            for lvl in (self._level_source(symbol) or []):  # type: ignore[misc]
                try:
                    p = float(lvl)
                except (TypeError, ValueError):
                    continue
                if lo <= p <= hi:
                    return True
            return False
        except Exception as exc:  # noqa: BLE001 — level source must never break routing
            logger.debug("[tick-state] level source error for {}: {}", symbol, exc)
            return False

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """A snapshot of the detector's counters (observability only)."""
        with self._lock:
            return {
                "symbols_tracked": len(self._states),
                "ticks_seen": self._ticks_seen,
                "changes_detected": self._changes_detected,
                "wakes": self._wakes,
                "throttled": self._throttled,
            }

    def reset(self, symbol: Optional[str] = None) -> None:
        """Drop tracked state for one symbol (or all when ``symbol is None``)."""
        with self._lock:
            if symbol is None:
                self._states.clear()
            else:
                self._states.pop(symbol, None)


def _clamp01(value: Any) -> float:
    """Coerce an EMA alpha into the open-ended (0, 1] range (fail-safe)."""
    try:
        a = float(value)
    except (TypeError, ValueError):
        return 0.05
    if a <= 0.0:
        return 0.0
    return min(1.0, a)


def _ema(prev: float, sample: float, alpha: float) -> float:
    """One exponential-moving-average step."""
    return (1.0 - alpha) * prev + alpha * sample


def _score(value: float, threshold: float) -> float:
    """Bounded change magnitude in [0.5, 1.0] for a value at/over its threshold.

    At the threshold the magnitude is 0.5; it reaches 1.0 at twice the
    threshold and saturates there.  This keeps the wake magnitude informative
    (bigger moves read stronger) while staying non-directional and bounded.
    """
    if threshold <= 0.0:
        return 1.0
    return min(1.0, 0.5 * value / threshold)


__all__ = ["TickStateChangeDetector"]
