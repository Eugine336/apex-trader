"""APEX TRADER — Hardened flip confirmation.

Replaces the weak 2-check flip confirmation (tick efficiency ≥ 0.30 + M5
non-opposition) that gated both the stop-out flip and the A1 direction-flip /
momentum-override paths. That old confirmation let flips fire on spread spikes,
Asian-session drift and low-volume wicks. This module hardens it into an
eight-check pipeline, run cheapest-first with a short-circuit on the first
failure:

    1. ATR-normalised magnitude  — the recent net directional move must clear
       ``flip_atr_move_threshold`` × the M5 ATR, so a single wick / spread
       spike can never satisfy the flip.
    2. Session-aware tick efficiency — the signed tick_momentum efficiency must
       reach a session-dependent floor (higher in the low-liquidity Asian
       session than in London / NY / overlap).
    3. Multi-TF structural non-opposition — M5 must not oppose the flip and,
       when required, M15 must not oppose either. Missing / ranging structure
       is permissive (never a block).
    4. Volume confirmation — recent M1 tick_volume vs its 20-bar average must
       clear a session-dependent ratio.
    5. Invalidation clean-break — the last CLOSED M1 candle must close cleanly
       beyond the invalidation level; a wick-only breach the close retreats
       from is rejected.
    6. Tick-rule order-flow delta — the recent stored ticks must show net
       aggressor pressure in the flip direction (delta ratio clears
       ``flip_delta_threshold``). Gated by ``flip_require_delta_confirmation``
       and degrades to a skip below 10 usable ticks.
    7. Fast-then-slow sequencing — M1 must have confirmed the flip direction
       FIRST and M5 must have confirmed on a later bar (a
       :class:`~entry.flip_sequence_tracker.FlipSequenceTracker` verdict). Gated
       by ``flip_require_sequence``.

The confirmer is STATELESS and fully injectable: every data source is supplied
as a callable (or a small store object), so the same logic runs live (fed from
the tick store / WorldModel / session context) and in the backtest (fed from
bar slices). Every threshold is resolved per-instrument via ``profile_param``
(InstrumentProfile first, then EntryConfig, then the literal default), so there
are zero hardcoded tuning values.

Graceful degradation: when a data source is unavailable (no ATR, no M1 frame,
no session, no structure, no invalidation level, no ticks, no sequence tracker)
the affected check is SKIPPED rather than failing — a missing read must never
block a flip on its own. Tick efficiency (check 2) is the one hard gate: an
unconfirmed / weak move is always refused. Any unexpected error fails closed
(``confirmed=False``) — a faulty read never flips a trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

import pandas as pd
from loguru import logger

from brain.instrument_profile import get_profile
from brain.market_data_utils import drop_forming_bar
from entry.tick_delta_analyzer import TickDeltaAnalyzer

# Literal fallbacks — used only when neither the InstrumentProfile nor the
# EntryConfig supplies a value. They mirror the InstrumentProfile field defaults
# so behaviour is identical whether a profile is resolved or not.
_DEFAULTS: dict[str, Any] = {
    "flip_atr_move_threshold": 0.3,
    "flip_tick_threshold_default": 0.30,
    "flip_tick_threshold_asian": 0.45,
    "flip_require_m15_non_opposition": True,
    "flip_volume_ratio_min": 1.0,
    "flip_volume_ratio_asian": 1.3,
    "flip_require_clean_break": True,
    "flip_delta_threshold": 0.2,
    "flip_delta_tick_count": 30,
    "flip_require_delta_confirmation": False,
    "flip_require_sequence": False,
}

_ASIAN = "ASIAN"


@dataclass(frozen=True)
class FlipConfirmation:
    """Result of a flip confirmation.

    ``confirmed`` is the go/no-go verdict, ``reason`` names the deciding check
    (``"confirmed"`` when all pass), and ``checks`` carries every intermediate
    value that was computed — for production logging and test assertions.
    """

    confirmed: bool
    reason: str
    checks: dict[str, Any]


def _default_profile_param(symbol: str, name: str, default: Any) -> Any:
    """Resolve a tuning knob: InstrumentProfile first, then the literal default.

    Used when the caller does not inject its own resolver. Fully guarded — a
    failed profile lookup degrades to the literal default.
    """
    try:
        prof = get_profile(symbol)
        val = getattr(prof, name, None) if prof is not None else None
        if val is not None:
            return val
    except Exception:  # noqa: BLE001 — tuning lookup must never break a flip
        pass
    return default


class FlipConfirmer:
    """Stateless eight-check flip confirmer.

    Construct once with the data-source callables (all optional and guarded);
    call :meth:`confirm` per flip decision. Because it holds no per-flip state
    it is safe to share across threads (the injected ``sequence_tracker`` owns
    its own thread-safe state).
    """

    def __init__(
        self,
        *,
        get_atr_pips: Optional[Callable[[str, str], float]] = None,
        get_tick_momentum: Optional[Callable[[str, str, float], float]] = None,
        get_tick_move_pips: Optional[Callable[[str, str, float], float]] = None,
        get_structure_trend: Optional[Callable[[str, str], str]] = None,
        get_m1_dataframe: Optional[Callable[[str], Optional[pd.DataFrame]]] = None,
        get_recent_ticks: Optional[Callable[[str, int], list]] = None,
        sequence_tracker: Optional[Any] = None,
        session_context: Optional[Any] = None,
        pip_size_lookup: Optional[Callable[[str], float]] = None,
        profile_param: Optional[Callable[[str, str, Any], Any]] = None,
        atr_tf: str = "M5",
    ) -> None:
        # ``(symbol, tf) -> atr_pips`` (0.0 / None ⇒ magnitude check skipped).
        self._get_atr_pips = get_atr_pips
        # ``(symbol, norm_dir, pip_size) -> efficiency`` in [-1, +1], signed for
        # the probed direction (the existing tick_momentum read).
        self._get_tick_momentum = get_tick_momentum
        # ``(symbol, norm_dir, pip_size) -> net move in pips`` signed for the
        # probed direction (None ⇒ magnitude check skipped).
        self._get_tick_move_pips = get_tick_move_pips
        # ``(symbol, tf) -> "BULLISH"|"BEARISH"|"RANGING"|"UNKNOWN"``. Wired from
        # the WorldModel store live; from bar slices in the backtest.
        self._get_structure_trend = get_structure_trend
        # ``(symbol) -> M1 DataFrame`` (must carry a ``tick_volume`` column for
        # the volume check; missing ⇒ volume / clean-break skipped).
        self._get_m1 = get_m1_dataframe
        # ``(symbol, count) -> list[Tick]`` for the tick-rule delta check
        # (None ⇒ delta check skipped). Wired to ``TickStore.get_recent`` live.
        self._get_recent_ticks = get_recent_ticks
        # FlipSequenceTracker-like object exposing ``is_confirmed(symbol, dir)``
        # for the fast-then-slow sequencing check (None ⇒ sequence skipped).
        self._sequence_tracker = sequence_tracker
        # SessionContext-like object exposing ``get_session()``.
        self._session_context = session_context
        self._pip_size = pip_size_lookup or (lambda _s: 0.0001)
        self._profile_param = profile_param or _default_profile_param
        self._atr_tf = atr_tf
        # Stateless helper for Check 6 — shared across all flips.
        self._delta_analyzer = TickDeltaAnalyzer()

    # ── Public API ────────────────────────────────────────────────────
    def confirm(
        self,
        symbol: str,
        direction: str,
        *,
        mode: str = "flip",
        invalidation_level: float = 0.0,
        checks_enabled: Optional[dict[str, bool]] = None,
    ) -> FlipConfirmation:
        """Run the eight checks for ``direction`` and return the verdict.

        ``direction`` is the direction being validated — the OPPOSITE side for a
        stop-out / A1 flip (``mode="flip"``), or the ORIGINAL zone side for the
        A1 momentum-override (``mode="agree"``). ``invalidation_level`` is the
        original zone's invalidation price (``0`` ⇒ clean-break check skipped).
        ``checks_enabled`` optionally disables individual checks by key
        (``atr`` / ``tick`` / ``m5`` / ``m15`` / ``volume`` / ``clean_break`` /
        ``delta`` / ``sequence``) — used by the backtest to toggle each check
        independently. All computed values are captured in ``checks`` regardless
        of the outcome.
        """
        checks: dict[str, Any] = {"mode": mode}
        want = str(direction).upper()
        norm_dir = "BUY" if want == "LONG" else "SELL"
        checks["direction"] = want
        try:
            pip_size = float(self._pip_size(symbol) or 0.0)
            session = self._session(symbol)
            checks["session"] = session

            # ── Check 1 — ATR-normalised magnitude ───────────────────
            if self._enabled(checks_enabled, "atr"):
                ok, reason = self._check_atr(symbol, norm_dir, pip_size, checks)
                if not ok:
                    return FlipConfirmation(False, reason, checks)

            # ── Check 2 — session-aware tick efficiency ──────────────
            if self._enabled(checks_enabled, "tick"):
                ok, reason = self._check_tick(symbol, norm_dir, pip_size, session, checks)
                if not ok:
                    return FlipConfirmation(False, reason, checks)
            else:
                # Still record the efficiency so the return signature the live
                # orchestrator preserves (tick_mom) stays populated.
                checks.setdefault("tick_efficiency", self._tick_efficiency(symbol, norm_dir, pip_size))

            # ── Check 3 — multi-TF structural non-opposition ─────────
            ok, reason = self._check_structure(symbol, want, checks_enabled, checks)
            if not ok:
                return FlipConfirmation(False, reason, checks)

            # M1 frame is shared by the volume + clean-break checks.
            m1_df = self._m1(symbol)

            # ── Check 4 — volume confirmation ────────────────────────
            if self._enabled(checks_enabled, "volume"):
                ok, reason = self._check_volume(symbol, session, m1_df, checks)
                if not ok:
                    return FlipConfirmation(False, reason, checks)

            # ── Check 5 — invalidation clean-break ───────────────────
            if self._enabled(checks_enabled, "clean_break"):
                ok, reason = self._check_clean_break(
                    symbol, want, invalidation_level, m1_df, checks,
                )
                if not ok:
                    return FlipConfirmation(False, reason, checks)

            # ── Check 6 — tick-rule order-flow delta ─────────────────
            if self._enabled(checks_enabled, "delta"):
                ok, reason = self._check_delta(symbol, want, checks)
                if not ok:
                    return FlipConfirmation(False, reason, checks)

            # ── Check 7 — fast-then-slow temporal sequencing ─────────
            if self._enabled(checks_enabled, "sequence"):
                ok, reason = self._check_sequence(symbol, want, checks)
                if not ok:
                    return FlipConfirmation(False, reason, checks)

            return FlipConfirmation(True, "confirmed", checks)
        except Exception as exc:  # noqa: BLE001 — a faulty read never flips a trade
            logger.debug("[flip-confirm] {} {} confirm failed: {}", symbol, want, exc)
            checks["error"] = str(exc)
            return FlipConfirmation(False, "error", checks)

    # ── Individual checks ─────────────────────────────────────────────
    def _check_atr(
        self, symbol: str, norm_dir: str, pip_size: float, checks: dict[str, Any],
    ) -> tuple[bool, str]:
        """Check 1 — the recent net move must clear a fraction of the ATR."""
        atr_pips = self._atr_pips(symbol)
        checks["atr_pips"] = atr_pips
        threshold = float(self._param(symbol, "flip_atr_move_threshold"))
        checks["flip_atr_move_threshold"] = threshold
        # Degrade gracefully: no ATR read or no magnitude source ⇒ skip (never
        # block a flip because volatility could not be measured).
        if atr_pips <= 0 or self._get_tick_move_pips is None:
            checks["atr"] = "skip"
            if atr_pips <= 0:
                logger.debug("[flip-confirm] {} ATR unavailable — magnitude check skipped", symbol)
            return True, ""
        move_pips = self._move_pips(symbol, norm_dir, pip_size)
        checks["move_pips"] = move_pips
        ratio = move_pips / atr_pips if atr_pips > 0 else 0.0
        checks["atr_move_ratio"] = ratio
        if ratio < threshold:
            checks["atr"] = "fail"
            return False, "atr_move"
        checks["atr"] = "pass"
        return True, ""

    def _check_tick(
        self, symbol: str, norm_dir: str, pip_size: float, session: str,
        checks: dict[str, Any],
    ) -> tuple[bool, str]:
        """Check 2 — signed tick efficiency vs a session-aware floor."""
        eff = self._tick_efficiency(symbol, norm_dir, pip_size)
        checks["tick_efficiency"] = eff
        if session == _ASIAN:
            threshold = float(self._param(symbol, "flip_tick_threshold_asian"))
        else:
            threshold = float(self._param(symbol, "flip_tick_threshold_default"))
        checks["tick_threshold"] = threshold
        if eff < threshold:
            checks["tick"] = "fail"
            return False, "tick_efficiency"
        checks["tick"] = "pass"
        return True, ""

    def _check_structure(
        self, symbol: str, want: str, checks_enabled: Optional[dict[str, bool]],
        checks: dict[str, Any],
    ) -> tuple[bool, str]:
        """Check 3 — M5 (and optionally M15) must not oppose the flip.

        A trend that actively points the OTHER way blocks the flip (BEARISH
        blocks a LONG, BULLISH blocks a SHORT). RANGING / UNKNOWN / missing
        structure is permissive.
        """
        if self._enabled(checks_enabled, "m5"):
            m5 = self._structure_trend(symbol, "M5")
            checks["m5_trend"] = m5
            if self._opposes(want, m5):
                checks["m5"] = "fail"
                return False, "m5_opposition"
            checks["m5"] = "pass"
        else:
            checks.setdefault("m5_trend", self._structure_trend(symbol, "M5"))

        require_m15 = bool(self._param(symbol, "flip_require_m15_non_opposition"))
        checks["flip_require_m15_non_opposition"] = require_m15
        if require_m15 and self._enabled(checks_enabled, "m15"):
            m15 = self._structure_trend(symbol, "M15")
            checks["m15_trend"] = m15
            if m15 in ("", "UNKNOWN", "RANGING"):
                checks["m15"] = "skip"  # missing / ranging structure is permissive
            elif self._opposes(want, m15):
                checks["m15"] = "fail"
                return False, "m15_opposition"
            else:
                checks["m15"] = "pass"
        return True, ""

    def _check_volume(
        self, symbol: str, session: str, m1_df: Optional[pd.DataFrame],
        checks: dict[str, Any],
    ) -> tuple[bool, str]:
        """Check 4 — recent M1 tick_volume vs its 20-bar average."""
        if m1_df is None or "tick_volume" not in getattr(m1_df, "columns", []):
            checks["volume"] = "skip"
            return True, ""
        try:
            closed = drop_forming_bar(m1_df)
            vols = pd.to_numeric(closed["tick_volume"], errors="coerce").dropna()
        except Exception:  # noqa: BLE001
            checks["volume"] = "skip"
            return True, ""
        if len(vols) < 20:
            checks["volume"] = "skip"
            return True, ""
        recent_n = min(2, len(vols))
        recent = float(vols.iloc[-recent_n:].mean())
        avg20 = float(vols.iloc[-20:].mean())
        if avg20 <= 0:
            checks["volume"] = "skip"
            return True, ""
        ratio = recent / avg20
        checks["volume_ratio"] = ratio
        if session == _ASIAN:
            min_ratio = float(self._param(symbol, "flip_volume_ratio_asian"))
        else:
            min_ratio = float(self._param(symbol, "flip_volume_ratio_min"))
        checks["volume_min_ratio"] = min_ratio
        if ratio < min_ratio:
            checks["volume"] = "fail"
            return False, "volume"
        checks["volume"] = "pass"
        return True, ""

    def _check_clean_break(
        self, symbol: str, want: str, invalidation_level: float,
        m1_df: Optional[pd.DataFrame], checks: dict[str, Any],
    ) -> tuple[bool, str]:
        """Check 5 — the last CLOSED M1 candle must close beyond invalidation."""
        require_clean = bool(self._param(symbol, "flip_require_clean_break"))
        checks["flip_require_clean_break"] = require_clean
        inv = float(invalidation_level or 0.0)
        if not require_clean or inv <= 0 or m1_df is None:
            checks["clean_break"] = "skip"
            return True, ""
        try:
            closed = drop_forming_bar(m1_df)
            if closed is None or len(closed) < 1:
                checks["clean_break"] = "skip"
                return True, ""
            last_close = float(closed.iloc[-1]["close"])
        except Exception:  # noqa: BLE001
            checks["clean_break"] = "skip"
            return True, ""
        checks["invalidation_level"] = inv
        checks["last_m1_close"] = last_close
        # LONG→SHORT flip: a clean bearish break closes BELOW invalidation.
        # SHORT→LONG flip: a clean bullish break closes ABOVE invalidation.
        clean = last_close < inv if want == "SHORT" else last_close > inv
        if not clean:
            checks["clean_break"] = "fail"
            return False, "clean_break"
        checks["clean_break"] = "pass"
        return True, ""

    def _check_delta(
        self, symbol: str, want: str, checks: dict[str, Any],
    ) -> tuple[bool, str]:
        """Check 6 — tick-rule order-flow delta must back the flip direction.

        Gated by ``flip_require_delta_confirmation`` (OFF by default). Reads the
        last ``flip_delta_tick_count`` stored ticks and requires the signed
        aggressor delta ratio to clear ``flip_delta_threshold`` in the flip
        direction. Degrades to a graceful skip when the feature is off, no tick
        source is wired, or fewer than 10 usable ticks are available.
        """
        require = bool(self._param(symbol, "flip_require_delta_confirmation"))
        checks["flip_require_delta_confirmation"] = require
        if not require or self._get_recent_ticks is None:
            checks["delta"] = "skip"
            return True, ""
        count = int(self._param(symbol, "flip_delta_tick_count"))
        threshold = float(self._param(symbol, "flip_delta_threshold"))
        checks["flip_delta_threshold"] = threshold
        try:
            ticks = self._get_recent_ticks(symbol, count) or []
        except Exception:  # noqa: BLE001 — an unavailable tick read never blocks
            checks["delta"] = "skip"
            return True, ""
        res = self._delta_analyzer.confirm(ticks, want, threshold)
        checks["delta_ratio"] = res.delta_ratio
        checks["delta_buy"] = res.buy_count
        checks["delta_sell"] = res.sell_count
        checks["delta_neutral"] = res.neutral_count
        if res.reason in ("insufficient_ticks", "unknown_direction"):
            checks["delta"] = "skip"
            return True, ""
        if not res.confirmed:
            checks["delta"] = "fail"
            return False, "delta"
        checks["delta"] = "pass"
        return True, ""

    def _check_sequence(
        self, symbol: str, want: str, checks: dict[str, Any],
    ) -> tuple[bool, str]:
        """Check 7 — the fast-then-slow sequence must be FULLY_CONFIRMED.

        Gated by ``flip_require_sequence`` (OFF by default). Consults the
        injected :class:`~entry.flip_sequence_tracker.FlipSequenceTracker`: the
        flip is allowed only when M1 confirmed first and M5 confirmed on a later
        bar for this symbol+direction. Skips when the feature is off or no
        tracker is wired.
        """
        require = bool(self._param(symbol, "flip_require_sequence"))
        checks["flip_require_sequence"] = require
        if not require or self._sequence_tracker is None:
            checks["sequence"] = "skip"
            return True, ""
        try:
            confirmed = bool(self._sequence_tracker.is_confirmed(symbol, want))
        except Exception:  # noqa: BLE001 — a faulty tracker read never blocks
            checks["sequence"] = "skip"
            return True, ""
        checks["sequence_confirmed"] = confirmed
        if not confirmed:
            checks["sequence"] = "fail"
            return False, "sequence"
        checks["sequence"] = "pass"
        return True, ""

    # ── Data-source helpers (all guarded) ─────────────────────────────
    def _param(self, symbol: str, name: str) -> Any:
        return self._profile_param(symbol, name, _DEFAULTS[name])

    @staticmethod
    def _enabled(checks_enabled: Optional[dict[str, bool]], key: str) -> bool:
        if not checks_enabled:
            return True
        return bool(checks_enabled.get(key, True))

    @staticmethod
    def _opposes(want: str, trend: str) -> bool:
        t = str(trend or "").upper()
        return (want == "LONG" and t == "BEARISH") or (want == "SHORT" and t == "BULLISH")

    def _atr_pips(self, symbol: str) -> float:
        if self._get_atr_pips is None:
            return 0.0
        try:
            return float(self._get_atr_pips(symbol, self._atr_tf) or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0

    def _move_pips(self, symbol: str, norm_dir: str, pip_size: float) -> float:
        if self._get_tick_move_pips is None:
            return 0.0
        try:
            return float(self._get_tick_move_pips(symbol, norm_dir, pip_size) or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0

    def _tick_efficiency(self, symbol: str, norm_dir: str, pip_size: float) -> float:
        if self._get_tick_momentum is None:
            return 0.0
        try:
            return float(self._get_tick_momentum(symbol, norm_dir, pip_size) or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0

    def _structure_trend(self, symbol: str, tf: str) -> str:
        if self._get_structure_trend is None:
            return "UNKNOWN"
        try:
            return str(self._get_structure_trend(symbol, tf) or "UNKNOWN")
        except Exception:  # noqa: BLE001
            return "UNKNOWN"

    def _m1(self, symbol: str) -> Optional[pd.DataFrame]:
        if self._get_m1 is None:
            return None
        try:
            return self._get_m1(symbol)
        except Exception:  # noqa: BLE001
            return None

    def _session(self, symbol: str) -> str:
        if self._session_context is None:
            return ""
        try:
            s = self._session_context.get_session()
            return str(getattr(s, "value", s) or "").upper()
        except Exception:  # noqa: BLE001
            return ""
