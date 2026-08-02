"""APEX TRADER — Entry Orchestrator (Phase 7).

Wires together ZoneWatcher → TickEntryDetector → M1CandleConfirmer
→ EntryGate, and emits the final entry decision.

The orchestrator is the single public interface for the entry plane.
Callers provide:
    - A ``WorldModelStore`` for zone extraction.
    - A ``pip_size_lookup`` callable ``(symbol) → float``.
    - An ``on_entry_decision`` callback that receives the final
      entry dict (or submits an OPEN intent to the IntentAggregator).
    - Optional external gate state callbacks for market_open, session,
      news, drawdown — decoupling the entry plane from broker details.

Usage::

    orchestrator = EntryOrchestrator(
        world_model_store=wm_store,
        pip_size_lookup=get_pip_size,
        on_entry_decision=lambda d: aggregator.submit(build_open_intent(d)),
    )
    # Wire events:
    tick_router.subscribe("tick", orchestrator.on_tick)
    candle_close_bus.subscribe("M1", orchestrator.on_m1_close)
    wm_store_bus.subscribe("world_model", orchestrator.on_world_model_update)
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

import pandas as pd
from loguru import logger

from brain.world_model import WorldModelStore
from brain.instrument_profile import get_profile
from entry.models import EntryConfig, EntryZone
from entry.zone_watcher import ZoneWatcher
from entry.tick_entry_detector import TickData, TickEntryDetector
from entry.m1_confirmation import M1CandleConfirmer
from entry.entry_gate import EntryGate
from entry.flip_confirmer import FlipConfirmer

if TYPE_CHECKING:
    from brain.candidate_models import Candidate


class EntryOrchestrator:
    """Top-level entry plane controller."""

    def __init__(
        self,
        world_model_store: WorldModelStore,
        config: Optional[EntryConfig] = None,
        pip_size_lookup: Optional[Callable[[str], float]] = None,
        on_entry_decision: Optional[Callable[[dict[str, Any]], None]] = None,
        is_market_open: Optional[Callable[[str], bool]] = None,
        is_session_active: Optional[Callable[[str], bool]] = None,
        is_news_clear: Optional[Callable[[str], bool]] = None,
        is_drawdown_ok: Optional[Callable[[], bool]] = None,
        is_instrument_known: Optional[Callable[[str], bool]] = None,
        get_spread_pips: Optional[Callable[[str], float]] = None,
        get_m1_dataframe: Optional[Callable[[str], Optional[pd.DataFrame]]] = None,
        get_tick_momentum: Optional[Callable[[str, str, float], float]] = None,
        on_gate_trace: Optional[Callable[..., None]] = None,
        gate_tuner: Optional[object] = None,
        pair_learner: Optional[object] = None,
        get_market_state: Optional[Callable[[str], Any]] = None,
        get_compression_score: Optional[Callable[[str], float]] = None,
        session_context: Optional[object] = None,
        get_currency_strength: Optional[Callable[..., Any]] = None,
        get_atr_pips: Optional[Callable[[str, str], float]] = None,
        get_tick_move_pips: Optional[Callable[[str, str, float], float]] = None,
    ) -> None:
        self._config = config or EntryConfig()
        self._pip_size = pip_size_lookup or (lambda _: 0.0001)
        self._on_entry = on_entry_decision
        self._on_gate_trace = on_gate_trace
        self._wm_store = world_model_store
        # PairLearner: drives cold-start score relaxation. During cold-start
        # (no trade history for a symbol) the entry bar is temporarily lowered
        # so trades can flow and the learning layer can bootstrap itself.
        # Once MIN_TRADES is reached, the learner's outcome data takes over.
        self._pair_learner = pair_learner
        # Global market-state read (compression detector). ACTIVE (Phase 2
        # Feature C): the orchestrator boosts zone conviction when the market is
        # COMPRESSING / EXPANDING and applies the Asian-compression caution gate.
        # Still fully guarded so an unwired/faulty detector degrades to neutral.
        self._get_market_state = get_market_state
        self._get_compression_score = get_compression_score
        # Global session classifier (brain/session_context.py). ACTIVE (Phase 2
        # Feature C): the orchestrator reads the current TradingSession for the
        # Asian-compression caution gate (session-aware SIZING is applied
        # downstream in the entry-decision pipeline). Guarded — an unwired
        # context degrades to no session shaping.
        self._session_context = session_context
        # USD strength reader for the DXY correlation filter (Phase 2 Feature D).
        # Contract: ``(symbol, lookback_bars) -> StrengthAnalysis | None``. When
        # USD strength opposes the trade direction on a USD-denominated symbol
        # the orchestrator applies a bounded conviction penalty (never a block).
        # Defaults to unwired (no DXY vote).
        self._get_currency_strength = get_currency_strength
        # Live sub-candle momentum for the trade direction → [-1, +1]
        # (signature: ``(symbol, norm_dir, pip_size) → float``). Used by the A1
        # flip confirmation so the flip rides real-time price, not a 60s-stale
        # M1 close. Defaults to neutral (0.0) when unwired.
        self._get_tick_momentum = get_tick_momentum or (lambda s, d, p: 0.0)
        # ATR (pips) reader ``(symbol, tf) -> float`` and signed net tick move
        # reader ``(symbol, norm_dir, pip_size) -> pips`` — the two magnitude
        # sources the hardened FlipConfirmer's ATR-normalised check needs. Both
        # default to neutral (0.0) so an unwired magnitude source degrades the
        # ATR check to a graceful skip rather than blocking a flip.
        self._get_atr_pips = get_atr_pips or (lambda s, tf: 0.0)
        self._get_tick_move_pips = get_tick_move_pips or (lambda s, d, p: 0.0)

        self._is_market_open = is_market_open or (lambda _: True)
        self._is_session_active = is_session_active or (lambda _: True)
        self._is_news_clear = is_news_clear or (lambda _: True)
        self._is_drawdown_ok = is_drawdown_ok or (lambda: True)
        self._is_instrument_known = is_instrument_known or (lambda _: True)
        self._get_spread = get_spread_pips or (lambda _: 0.5)
        self._get_m1 = get_m1_dataframe

        self._zone_watcher = ZoneWatcher(world_model_store, self._config)
        self._m1_confirmer = M1CandleConfirmer(self._config, self._pip_size)
        self._gate = EntryGate(self._config, gate_tuner=gate_tuner)
        self._tick_detector = TickEntryDetector(
            zone_watcher=self._zone_watcher,
            config=self._config,
            on_zone_touch=self._handle_zone_touch,
            pip_size_lookup=self._pip_size,
        )

        self._confirming: dict[str, dict] = {}
        self._lock = threading.Lock()

        # ── Phase 2 Feature A: stop-out flip tracking ────────────────────
        # Per-symbol monotonic time of the last flip (cooldown enforcement) and
        # per-zone flip counter (whipsaw guard). The zone counter is keyed by
        # (symbol, zone_type, timeframe) and reset to zero on a fresh zone touch
        # so a genuinely new approach to a zone starts with a clean flip budget.
        self._last_flip_time: dict[str, float] = {}
        self._zone_flip_counts: dict[tuple[str, str, str], int] = {}

        self._stats = {
            "zones_extracted": 0,
            "zone_touches": 0,
            "m1_confirmations": 0,
            "m1_rejections": 0,
            "gate_passes": 0,
            "gate_failures": 0,
            "entries_emitted": 0,
        }

        # ── Hardened flip confirmation (entry/flip_confirmer.py) ──────────
        # The single stateless five-check confirmer used by BOTH the stop-out
        # flip (evaluate_stopout_flip) and the A1 direction-flip / momentum-
        # override paths. Fed the live data sources: ATR + signed tick move
        # (magnitude), tick_momentum (efficiency), the WorldModel structure
        # trend per TF, the M1 frame (volume + clean-break) and the session
        # context. Every threshold resolves per-instrument via _profile_param.
        self._flip_confirmer = FlipConfirmer(
            get_atr_pips=self._get_atr_pips,
            get_tick_momentum=self._get_tick_momentum,
            get_tick_move_pips=self._get_tick_move_pips,
            get_structure_trend=self._wm_structure_trend,
            get_m1_dataframe=self._get_m1,
            session_context=self._session_context,
            pip_size_lookup=self._pip_size,
            profile_param=self._profile_param,
        )

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    @property
    def zone_watcher(self) -> ZoneWatcher:
        return self._zone_watcher

    @property
    def tick_detector(self) -> TickEntryDetector:
        return self._tick_detector

    def on_world_model_update(self, symbol: str) -> None:
        """Handle WorldModel publish for a symbol."""
        try:
            self._zone_watcher.on_world_model_update(symbol)
            zones = self._zone_watcher.get_active_zones(symbol)
            self._stats["zones_extracted"] += len(zones)
        except Exception:
            logger.exception("[entry-orch] WorldModel update failed for {}", symbol)

    def on_tick(self, tick: TickData) -> None:
        """Process an incoming tick for entry zone detection."""
        try:
            self._tick_detector.on_tick(tick)
        except Exception:
            logger.exception("[entry-orch] tick processing failed for {}", tick.symbol)

    def on_m1_close(self, symbol: str) -> None:
        """Handle M1 candle close for pending confirmations."""
        with self._lock:
            info = self._confirming.get(symbol)
            if info is None:
                return
            info = dict(info)

        m1_df = None
        if self._get_m1 is not None:
            try:
                m1_df = self._get_m1(symbol)
            except Exception:
                logger.exception("[entry-orch] failed to fetch M1 for {}", symbol)

        if m1_df is None:
            logger.debug("[entry-orch] no M1 data for {}", symbol)
            return

        zone = info["zone"]
        direction = info["direction"]

        result = self._m1_confirmer.on_m1_close(symbol, direction, zone, m1_df)

        if result.confirmed:
            self._stats["m1_confirmations"] += 1
            self._tick_detector.mark_confirmed(symbol)
            self._run_gates_and_emit(symbol, info, m1_df)
        elif result.method == "none" and "timeout" in result.reason.lower():
            self._stats["m1_rejections"] += 1
            logger.info(
                "[entry-orch] {} M1 confirmation timed out", symbol,
            )
            self._tick_detector.cancel_pending(symbol, "M1 timeout")
            with self._lock:
                self._confirming.pop(symbol, None)

    def reset(self) -> None:
        """Reset all state."""
        self._zone_watcher.clear()
        self._tick_detector.reset()
        self._m1_confirmer.reset()
        with self._lock:
            self._confirming.clear()
        self._last_flip_time.clear()
        self._zone_flip_counts.clear()

    def _handle_zone_touch(self, pending) -> None:
        """Callback from TickEntryDetector when a zone touch is detected."""
        self._stats["zone_touches"] += 1
        # Phase 2 Feature A: a fresh touch of a zone resets its flip budget — a
        # new approach is a new opportunity, not a continuation of a prior
        # whipsaw sequence.
        try:
            zone = pending.zone
            key = (
                pending.symbol,
                getattr(getattr(zone, "zone_type", None), "value", ""),
                getattr(zone, "timeframe", "") or "",
            )
            self._zone_flip_counts.pop(key, None)
        except Exception:  # noqa: BLE001 — counter reset must never break a touch
            pass
        with self._lock:
            self._confirming[pending.symbol] = {
                "symbol": pending.symbol,
                "direction": pending.direction,
                "zone": pending.zone,
                "touch_price": pending.touch_price,
                "touch_time": pending.touch_time,
            }

    def _match_candidate_for_zone(
        self, symbol: str, direction: str, timeframe: str,
    ) -> "Optional[Candidate]":
        """Find the ranked Candidate that best explains this zone, or ``None``.

        A zone is the structural "price comes to the setup" expression of an
        idea; the WorldModel's ranked candidates are the same intelligence
        expressed as independent (direction × timeframe-class) clusters. Matching
        the fired zone back to its candidate attaches full provenance to the
        emitted decision so portfolio selection (Session 3), candidate-scoped
        management (Session 4) and the learning loop can track one idea from the
        zone path the same way they do from the consensus path.

        Prefers a candidate whose timeframe class matches the zone's timeframe;
        falls back to any same-direction candidate. Best-effort and fully
        guarded — a miss simply returns ``None`` and the zone entry proceeds with
        no candidate attached (legacy behaviour).
        """
        if self._wm_store is None:
            return None
        try:
            wm = self._wm_store.get(symbol)
            if wm is None:
                return None
            cands = (
                wm.candidates_list()
                if hasattr(wm, "candidates_list")
                else list(getattr(wm, "candidates", ()) or [])
            )
            if not cands:
                return None

            want = direction.upper()
            same_dir = [
                c for c in cands
                if str(getattr(c, "direction", "")).upper() == want
            ]
            if not same_dir:
                return None

            from brain.candidate_models import Candidate
            from brain.opportunity_ranker import classify_timeframe

            zone_class = classify_timeframe("", (), (), timeframe or "")
            best = None
            for c in same_dir:
                if str(getattr(c, "timeframe_class", "")).upper() == zone_class:
                    best = c
                    break
            if best is None:
                best = same_dir[0]

            regime = ""
            try:
                rbtf = wm.regime_by_tf()
                regime = str(
                    rbtf.get("H1")
                    or rbtf.get("H4")
                    or next(iter(rbtf.values()), "")
                    or ""
                )
            except Exception:
                regime = ""
            return Candidate.from_opportunity(best, regime_context=regime)
        except Exception as exc:
            logger.debug(
                "[entry-orch] candidate match failed for {}: {}", symbol, exc,
            )
            return None

    def _profile_param(self, symbol: str, name: str, default: Any) -> Any:
        """Resolve a tuning value: InstrumentProfile first, then EntryConfig,
        then the literal ``default``.

        Mirrors the resolution order used by CompressionDetector / SessionContext
        so every Phase 2 knob is per-instrument tunable via InstrumentProfile
        while EntryConfig supplies the global fallback. Fully guarded — a failed
        profile lookup degrades to the EntryConfig value or the literal default.
        """
        try:
            prof = get_profile(symbol)
        except Exception:  # noqa: BLE001 — tuning lookup must never break entry
            prof = None
        if prof is not None:
            val = getattr(prof, name, None)
            if val is not None:
                return val
        val = getattr(self._config, name, None)
        return default if val is None else val

    def _htf_alignment(self, symbol: str, direction: str) -> Optional[float]:
        """Signed HTF alignment for a trade direction from the WorldModel bias.

        Returns +score/100 when the bias direction supports the trade,
        -score/100 when it opposes, 0.0 when the bias is undirected, and
        ``None`` when no WorldModel/bias is available (gate stays permissive).
        """
        if self._wm_store is None:
            return None
        try:
            wm = self._wm_store.get(symbol)
            if wm is None:
                return None
            bias = wm.bias_dict()
            bdir = str(bias.get("direction", "") or "").upper()
            score = float(bias.get("score", 0) or 0)
            if bdir not in ("LONG", "SHORT"):
                return 0.0
            signed = max(0.0, min(100.0, score)) / 100.0
            return signed if bdir == direction.upper() else -signed
        except Exception as exc:
            logger.debug("[entry-orch] alignment read failed for {}: {}", symbol, exc)
            return None

    def _entry_probabilities(self, symbol: str) -> tuple[float, float]:
        """Absolute (long, short) probabilities from the WorldModel bias.

        Reads the Phase 3 probabilistic evidence model's ``long_probability`` /
        ``short_probability`` from the stored bias dict — the same source the
        ``_htf_alignment`` read uses. These are direction-agnostic (the EV gate
        maps them to p_win/p_loss for the trade direction), so the same pair is
        passed whether the entry is with- or counter-trend, and survives a
        direction flip unchanged. Returns ``(0.0, 0.0)`` when no
        WorldModel/bias is available — the EV gate then sees no probabilistic
        edge (EV ≤ 0) and rejects, which is the opportunistic-correct "no edge,
        no trade" behaviour.
        """
        if self._wm_store is None:
            return 0.0, 0.0
        try:
            wm = self._wm_store.get(symbol)
            if wm is None:
                return 0.0, 0.0
            bias = wm.bias_dict()
            long_p = float(bias.get("long_probability", 0.0) or 0.0)
            short_p = float(bias.get("short_probability", 0.0) or 0.0)
            return long_p, short_p
        except Exception as exc:
            logger.debug(
                "[entry-orch] probability read failed for {}: {}", symbol, exc,
            )
            return 0.0, 0.0

    def _entry_momentum(self, symbol: str, direction: str) -> Optional[float]:
        """Signed live momentum for a trade direction from the WorldModel.

        Reads the unbiased ``momentum`` module vote(s) (RSI+MACD on M5/H1) and
        signs them relative to the trade: +confidence when the momentum vote
        agrees with ``direction``, −confidence when it opposes. Returns the
        clamped sum in [-1, +1] (positive = momentum supports the direction), or
        ``None`` when no momentum vote is available (caller stays permissive).
        """
        if self._wm_store is None:
            return None
        try:
            wm = self._wm_store.get(symbol)
            if wm is None:
                return None
            votes = (
                wm.votes_list()
                if hasattr(wm, "votes_list")
                else list(getattr(wm, "votes", ()) or [])
            )
            want = str(direction).upper()
            signal = 0.0
            found = False
            for v in votes:
                if str(getattr(v, "module", "")).lower() != "momentum":
                    continue
                vdir = str(getattr(v, "direction", "")).upper()
                if vdir not in ("LONG", "SHORT"):
                    continue
                conf = max(0.0, min(1.0, float(getattr(v, "confidence", 0.0) or 0.0)))
                found = True
                signal += conf if vdir == want else -conf
            if not found:
                return None
            return max(-1.0, min(1.0, signal))
        except Exception as exc:
            logger.debug("[entry-orch] momentum read failed for {}: {}", symbol, exc)
            return None

    @staticmethod
    def _entry_posture(
        alignment: Optional[float], momentum: Optional[float],
    ) -> str:
        """Lightweight situation posture for the entry (mirrors the situation
        engine's alignment/momentum label branches).

        Both inputs are signed for the trade direction. Returns ``"MIXED"`` for a
        no-clear-edge setup (the case the gate tightens), or a more specific
        label when one applies. Empty string when inputs are unavailable so the
        gate keeps its permissive default.
        """
        if alignment is None or momentum is None:
            return ""
        a = float(alignment)
        m = float(momentum)
        if a > 0.4 and m > 0.1:
            return "TREND_CONTINUATION"
        if a > 0.3 and m < -0.2:
            return "COUNTER_MOMENTUM"
        if abs(a) < 0.2:
            return "RANGE_ENTRY"
        if a < -0.3:
            return "COUNTER_TREND"
        return "MIXED"

    def _m1_confirms_direction(
        self, direction: str, zone, m1_df: pd.DataFrame, pip_size: float,
    ) -> bool:
        """True when the M1 frame structurally/momentum-confirms ``direction``.

        Re-uses the M1 confirmer's stateless checks (structure then momentum)
        without touching its per-symbol candle counter, so it can be probed for
        the OPPOSITE direction during the A1 flip decision without disturbing the
        live confirmation state.
        """
        try:
            r = M1CandleConfirmer._check_structure(m1_df, direction, pip_size)
            if r.confirmed:
                return True
            r = M1CandleConfirmer._check_momentum(m1_df, direction, zone, pip_size)
            return bool(r.confirmed)
        except Exception:
            return False

    def _wm_structure_trend(self, symbol: str, tf: str) -> str:
        """Confirmed structure trend string for ``tf`` from the WorldModel.

        Returns "BULLISH"/"BEARISH"/"RANGING" or "UNKNOWN" when no WorldModel /
        structure is available. Wired into the :class:`FlipConfirmer` as its
        per-TF structure reader. Never raises — an unavailable read degrades to
        "UNKNOWN" (permissive), so a missing WorldModel never blocks a flip.
        """
        try:
            if self._wm_store is None:
                return "UNKNOWN"
            wm = self._wm_store.get(symbol)
            if wm is None:
                return "UNKNOWN"
            sa = wm.structure_by_tf().get(tf)
            if sa is None:
                return "UNKNOWN"
            return str(getattr(getattr(sa, "trend", None), "value", "") or "UNKNOWN")
        except Exception:  # noqa: BLE001 — a faulty read never blocks a flip
            return "UNKNOWN"

    def _tick_m5_confirms_flip(
        self, opposite: str, symbol: str, invalidation_level: float = 0.0,
    ) -> tuple[bool, float, str]:
        """Confirm a direction flip via the hardened five-check FlipConfirmer.

        Replaces the old 2-check confirmation (tick efficiency ≥ threshold + M5
        non-opposition) with the full :class:`FlipConfirmer` pipeline in
        ``mode="flip"``: ATR-normalised magnitude, session-aware tick
        efficiency, M5 + M15 non-opposition, M1 volume confirmation and the
        invalidation clean-break. ``invalidation_level`` (the original zone's
        invalidation price) enables the clean-break check for the stop-out flip
        path; the A1 direction-flip passes ``0`` and the clean-break is skipped.

        Returns ``(confirmed, tick_mom, m5_trend)`` — the tick efficiency and M5
        trend are read back out of the confirmation's ``checks`` dict so the
        caller's existing return signature and logging are preserved. Fails
        closed on any error (the confirmer never raises).
        """
        result = self._flip_confirmer.confirm(
            symbol, opposite, mode="flip",
            invalidation_level=float(invalidation_level or 0.0),
        )
        tick_mom = float(result.checks.get("tick_efficiency", 0.0) or 0.0)
        m5_trend = str(result.checks.get("m5_trend", "UNKNOWN") or "UNKNOWN")
        logger.debug(
            "[flip-confirm] {} flip→{} {} ({}) checks={}",
            symbol, opposite,
            "confirmed" if result.confirmed else "rejected",
            result.reason, result.checks,
        )
        return result.confirmed, tick_mom, m5_trend

    def _tick_m5_agree_direction(
        self, direction: str, symbol: str,
    ) -> tuple[bool, float, str]:
        """Confirm the ORIGINAL zone ``direction`` via the FlipConfirmer.

        The A1 momentum-override path: when the (lagging) blended momentum reads
        as opposing the zone but the leading signals may still support it, this
        runs the SAME hardened five-check pipeline in ``mode="agree"`` for the
        original direction. The entry proceeds only when every non-skipped check
        confirms the zone side — replacing the old permissive
        "tick_momentum > 0 AND M5 non-opposing" rule that let entries through on
        micro-drift.

        Returns ``(agree, tick_mom, m5_trend)`` so the caller can log the actual
        values. Fails closed on any error.
        """
        result = self._flip_confirmer.confirm(symbol, direction, mode="agree")
        tick_mom = float(result.checks.get("tick_efficiency", 0.0) or 0.0)
        m5_trend = str(result.checks.get("m5_trend", "UNKNOWN") or "UNKNOWN")
        logger.debug(
            "[flip-confirm] {} agree {} {} ({}) checks={}",
            symbol, direction,
            "confirmed" if result.confirmed else "rejected",
            result.reason, result.checks,
        )
        return result.confirmed, tick_mom, m5_trend

    def _flip_zone(self, zone: EntryZone, new_direction: str, entry_price: float = 0.0) -> EntryZone:
        """Mirror a zone to the opposite trade direction.

        The stop (invalidation) is mirrored around the ENTRY (touch) price so a
        flipped SHORT always stops ABOVE entry and a flipped LONG always stops
        BELOW entry, preserving the original risk distance. Mirroring around the
        zone alone is unsafe: when price has already run past the zone, the
        zone-relative invalidation can land on the wrong side of the entry,
        producing an inverted SL/TP that the gate rejects. Anchoring on the
        entry price guarantees a valid SHORT (SL above) / LONG (SL below).
        A flipped trade is by construction WITH momentum, so the counter-trend
        conviction haircut is undone (conviction restored to its un-penalised
        base) and ``is_counter_trend`` cleared — the flip must not double-penalise
        an idea that now agrees with the move.
        """
        from dataclasses import replace

        orig_inv = float(getattr(zone, "invalidation_level", 0.0) or 0.0)
        if entry_price and entry_price > 0 and orig_inv > 0:
            # Mirror the stop around the entry: new_sl = 2*entry − original_sl.
            # Preserves the risk distance and flips it to the correct side.
            inv = 2.0 * entry_price - orig_inv
            # Defensive: ensure the mirrored stop is on the correct side of
            # entry for the new direction (it will be unless risk was zero).
            if str(new_direction).upper() == "LONG" and inv >= entry_price:
                inv = entry_price - abs(entry_price - orig_inv)
            elif str(new_direction).upper() == "SHORT" and inv <= entry_price:
                inv = entry_price + abs(entry_price - orig_inv)
        else:
            # Fallback (no usable entry): mirror around the zone geometry.
            zone_size = abs(zone.top - zone.bottom)
            buffer = zone_size * 0.5
            inv = (
                zone.bottom - buffer
                if str(new_direction).upper() == "LONG"
                else zone.top + buffer
            )

        conviction = int(getattr(zone, "conviction", 0) or 0)
        if getattr(zone, "is_counter_trend", False):
            # Undo the same haircut the zone was penalised with: the EV-gate
            # softer multiplier (0.85) when the EV gate is enabled, else the
            # legacy 0.70. A flipped trade is WITH momentum, so the counter-trend
            # penalty must not linger on the restored conviction.
            if getattr(self._config, "ev_gate_enabled", False):
                mult = float(
                    getattr(self._config, "counter_trend_conviction_mult_ev", 1.0) or 1.0
                )
            else:
                mult = float(
                    getattr(self._config, "counter_trend_conviction_mult", 1.0) or 1.0
                )
            if 0.0 < mult < 1.0:
                conviction = min(100, int(round(conviction / mult)))
        return replace(
            zone,
            direction=str(new_direction).upper(),
            invalidation_level=inv,
            conviction=conviction,
            is_counter_trend=False,
        )

    def _derive_targets(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        sl: float,
    ) -> tuple[float, float]:
        """Take-profit targets from MARKET structure, not a hardcoded multiple.

        The opportunistic-trading rewire reads the next structural levels ahead
        of price (FVG / order block / liquidity pool from the WorldModel) and
        uses them as TP1 (nearest qualifying target) and TP2 (next structural
        level beyond it). A target only qualifies if it is at least
        ``min_structural_rr`` × risk away, so the trade's reward:risk is set by
        what the market is presenting rather than a fixed R-multiple.

        Falls back to a risk-multiple target (a safety net) only when the brain
        exposes no structure ahead — the downstream EntryEngine still applies
        its ATR-based SL/TP refinement ("tighter wins") afterwards.
        """
        risk = abs(entry_price - sl)
        if risk <= 0:
            risk = entry_price * 0.001 if entry_price > 0 else 1.0
        is_long = str(direction).upper() == "LONG"
        min_rr = getattr(self._config, "min_structural_rr", 1.0)
        min_distance = risk * max(min_rr, 0.0)

        targets: list[float] = []
        if self._wm_store is not None:
            try:
                wm = self._wm_store.get(symbol)
                if wm is not None:
                    targets = wm.get_structural_targets(
                        direction, entry_price, min_distance=min_distance,
                    )
            except Exception as exc:
                logger.debug(
                    "[entry-orch] structural targets failed for {}: {}", symbol, exc,
                )
                targets = []

        if targets:
            tp1 = targets[0]
            # TP2 = next structural level beyond TP1 (else extend by one more R).
            tp2 = next((t for t in targets if abs(t - entry_price) > abs(tp1 - entry_price)), None)
            if tp2 is None:
                extra = abs(tp1 - entry_price) + risk
                tp2 = entry_price + extra if is_long else entry_price - extra
            return tp1, tp2

        # Safety-net fallback: no structure ahead → risk-multiple targets. The
        # multiples respect the structural-R:R floor so the gate still passes.
        rr1 = max(min_rr, 1.5)
        rr2 = max(min_rr * 2.0, 3.0)
        if is_long:
            return entry_price + risk * rr1, entry_price + risk * rr2
        return entry_price - risk * rr1, entry_price - risk * rr2

    def evaluate_stopout_flip(
        self,
        symbol: str,
        closed_direction: str,
        closed_entry_price: float,
        closed_stop_loss: float,
        *,
        current_price: float = 0.0,
        conviction: int = 0,
        zone_type: str = "",
        timeframe: str = "",
        invalidation_level: float = 0.0,
        emit: bool = True,
    ) -> dict[str, Any]:
        """Phase 2 Feature A — evaluate (and emit) a flip after a stop-out.

        Called by the event-driven system's ``_on_trade_closed`` when a trade
        exits on a stop-loss fill. Reuses the existing live-tick + M5 flip
        confirmation (:meth:`_tick_m5_confirms_flip`) so the flip only rides a
        clean, current move that structure does not oppose. Guards (all resolved
        InstrumentProfile-first, then EntryConfig):

        * ``stopout_flip_enabled`` toggles the feature.
        * ``stopout_flip_cooldown_s`` — minimum seconds between flips on the same
          symbol (rapid-flip / spread-burn guard).
        * ``stopout_flip_max_per_zone`` — max flips one zone may spawn before it
          is exhausted (whipsaw death-spiral guard); the per-zone counter resets
          on a fresh zone touch (see :meth:`_handle_zone_touch`).

        When confirmed the emitted decision is routed through the SAME
        ``on_entry_decision`` callback as every other entry, so it runs the full
        downstream pipeline (compliance, portfolio risk, sizing) — no shortcuts.

        Returns ``{confirmed, direction, tick_mom, m5_trend, decision, reason}``
        for logging and testing. Never raises.
        """
        opposite = "SHORT" if str(closed_direction).upper() == "LONG" else "LONG"
        result: dict[str, Any] = {
            "confirmed": False,
            "direction": opposite,
            "tick_mom": 0.0,
            "m5_trend": "UNKNOWN",
            "decision": None,
            "reason": "",
        }

        if not bool(self._profile_param(symbol, "stopout_flip_enabled", True)):
            result["reason"] = "disabled"
            return result

        zone_key = (symbol, str(zone_type or ""), str(timeframe or ""))
        cooldown = float(self._profile_param(symbol, "stopout_flip_cooldown_s", 30.0))
        max_per_zone = int(self._profile_param(symbol, "stopout_flip_max_per_zone", 2))

        now = time.monotonic()
        last = self._last_flip_time.get(symbol)
        if last is not None and (now - last) < cooldown:
            result["reason"] = "cooldown"
            logger.info(
                "[stopout-flip] {} SL hit on {} → flip check: rejected "
                "(cooldown {:.0f}s not elapsed)",
                symbol, closed_direction, cooldown,
            )
            return result

        flips_from_zone = self._zone_flip_counts.get(zone_key, 0)
        if flips_from_zone >= max_per_zone:
            result["reason"] = "zone_exhausted"
            logger.info(
                "[stopout-flip] {} SL hit on {} → flip check: rejected "
                "(zone exhausted {}/{} flips)",
                symbol, closed_direction, flips_from_zone, max_per_zone,
            )
            return result

        confirmed, tick_mom, m5_trend = self._tick_m5_confirms_flip(
            opposite, symbol, invalidation_level=float(invalidation_level or 0.0),
        )
        result["tick_mom"] = tick_mom
        result["m5_trend"] = m5_trend
        logger.info(
            "[stopout-flip] {} SL hit on {} → flip check: {} (tick_mom={:+.2f} M5={})",
            symbol, closed_direction, "confirmed" if confirmed else "rejected",
            tick_mom, m5_trend,
        )
        if not confirmed:
            result["reason"] = "unconfirmed"
            return result

        # Build the opposite-direction entry decision. The flip enters at the
        # current price (the stop-out fill) with the original risk distance
        # mirrored to the correct side, and targets derived from structure.
        flip_entry = (
            float(current_price)
            if current_price and current_price > 0
            else float(closed_entry_price)
        )
        risk = abs(float(closed_entry_price) - float(closed_stop_loss))
        if risk <= 0:
            risk = flip_entry * 0.001 if flip_entry > 0 else 1.0
        sl = flip_entry - risk if opposite == "LONG" else flip_entry + risk
        tp1, tp2 = self._derive_targets(symbol, opposite, flip_entry, sl)

        pip_size = self._pip_size(symbol)
        risk_pips = (risk / pip_size) if pip_size > 0 else 0.0

        decision = {
            "symbol": symbol,
            "direction": opposite,
            "entry_price": flip_entry,
            "stop_loss": sl,
            "tp1": tp1,
            "tp2": tp2,
            "conviction": int(conviction or 0),
            "zone_type": str(zone_type or ""),
            "timeframe": str(timeframe or ""),
            # Entry-source attribution: a stop-out flip is its own path so the
            # learning loop can split its win rate from zone / consensus entries.
            "source": "stopout_flip",
            "risk_pips": risk_pips,
            "has_sweep": False,
            "is_counter_trend": False,
            "bias_direction": "",
        }
        result["decision"] = decision
        result["confirmed"] = True
        result["reason"] = "confirmed"

        self._zone_flip_counts[zone_key] = flips_from_zone + 1
        self._last_flip_time[symbol] = now
        self._stats["stopout_flips"] = self._stats.get("stopout_flips", 0) + 1

        if emit and self._on_entry is not None:
            try:
                self._on_entry(decision)
            except Exception:
                logger.exception("[stopout-flip] on_entry callback failed for {}", symbol)
        return result

    def _read_market_state(self, symbol: str) -> str:
        """Current MarketState value string for ``symbol`` ("" if unwired)."""
        if self._get_market_state is None:
            return ""
        try:
            state = self._get_market_state(symbol)
            return str(getattr(state, "value", state) or "")
        except Exception:  # noqa: BLE001 — a faulty read never affects an entry
            return ""

    def _current_session(self) -> str:
        """Current TradingSession value string ("" if unwired)."""
        if self._session_context is None:
            return ""
        try:
            session = self._session_context.get_session()
            return str(getattr(session, "value", session) or "")
        except Exception:  # noqa: BLE001
            return ""

    def _conviction_multiplier(self, symbol: str, market_state: str) -> float:
        """Phase 2 Feature C — conviction boost from the compression state.

        COMPRESSING boosts by ``compression_conviction_boost`` (a squeeze
        precedes a breakout); EXPANDING boosts by ``expansion_conviction_boost``
        (the breakout is resolving). Any other state is neutral (1.0).
        """
        state = (market_state or "").upper()
        if state == "COMPRESSING":
            return float(self._profile_param(symbol, "compression_conviction_boost", 1.2))
        if state == "EXPANDING":
            return float(self._profile_param(symbol, "expansion_conviction_boost", 1.5))
        return 1.0

    def _dxy_opposition_penalty(self, symbol: str, direction: str) -> float:
        """Phase 2 Feature D — conviction penalty when USD strength opposes.

        Only active on USD-denominated symbols (XAUUSD, forex USD pairs) when
        ``dxy_filter_enabled``. Reads the injected currency-strength meter for
        the USD ranking: USD strengthening while LONG gold (or weakening while
        SHORT gold) is opposition → returns ``dxy_opposition_penalty`` (a
        bounded conviction haircut, NEVER a block). Returns 0.0 otherwise or on
        any read failure. This is a warning vote, not a gate.
        """
        if self._get_currency_strength is None:
            return 0.0
        if not bool(self._profile_param(symbol, "dxy_filter_enabled", True)):
            return 0.0
        if "USD" not in symbol.upper():
            return 0.0
        try:
            lookback = int(self._profile_param(symbol, "dxy_lookback_bars", 20))
            analysis = self._get_currency_strength(symbol, lookback)
            if analysis is None:
                return 0.0
            usd = None
            for r in getattr(analysis, "rankings", []) or []:
                if str(getattr(r, "currency", "")).upper() == "USD":
                    usd = r
                    break
            if usd is None:
                return 0.0
            momentum = str(getattr(usd, "momentum", "") or "").upper()
            score = float(getattr(usd, "score", 0.0) or 0.0)
            # USD strengthening: RISING momentum, or a positive score when
            # momentum is unavailable / STABLE. Weakening: FALLING / negative.
            if momentum == "RISING":
                strengthening = True
            elif momentum == "FALLING":
                strengthening = False
            else:
                strengthening = score > 0.0
            want = str(direction).upper()
            opposes = (
                (want == "LONG" and strengthening)
                or (want == "SHORT" and not strengthening)
            )
            if not opposes:
                return 0.0
            return float(self._profile_param(symbol, "dxy_opposition_penalty", 0.15))
        except Exception:  # noqa: BLE001 — the DXY vote never breaks an entry
            return 0.0

    def _shaped_conviction(
        self,
        symbol: str,
        direction: str,
        base_score: int,
        market_state: str,
        session: str,
    ) -> Optional[int]:
        """Phase 2 Features C+D — shape a base conviction for the score gate.

        Applies, in order: the Asian-compression caution gate (returns ``None``
        to vote the entry skipped), the COMPRESSING/EXPANDING conviction boost,
        and the DXY opposition penalty. Returns the bounded shaped score, or
        ``None`` when the caution gate votes to skip. Shared by the live entry
        path (:meth:`_run_gates_and_emit`) and the pre-staging gate
        (:meth:`evaluate_zone_gates`) so both use identical shaping.
        """
        score = int(base_score)
        state = (market_state or "").upper()
        sess = (session or "").upper()

        # Asian-compression caution: a COMPRESSING market in the low-liquidity
        # ASIAN session is dangerous chop — skip unless conviction clears the
        # configured minimum (checked on the pre-boost score so the boost below
        # can't be what pushes a weak Asian-chop setup over the line).
        if state == "COMPRESSING" and sess == "ASIAN":
            asian_min = int(
                self._profile_param(symbol, "asian_compression_min_conviction", 80)
            )
            if score <= asian_min:
                logger.info(
                    "[entry-orch] {} {} SKIPPED — Asian+COMPRESSING caution "
                    "(conviction {} <= {})",
                    symbol, direction, score, asian_min,
                )
                self._stats["asian_compression_skips"] = (
                    self._stats.get("asian_compression_skips", 0) + 1
                )
                return None

        boost = self._conviction_multiplier(symbol, state)
        if boost != 1.0:
            boosted = int(round(score * boost))
            logger.info(
                "[entry-orch] {} {} conviction {} -> {} (x{:.2f} for {})",
                symbol, direction, score, boosted, boost, state or "?",
            )
            score = boosted

        penalty = self._dxy_opposition_penalty(symbol, direction)
        if penalty > 0.0:
            penalised = int(round(score * (1.0 - penalty)))
            logger.info(
                "[entry-orch] {} {} DXY opposition — conviction {} -> {} "
                "(-{:.0%}, USD opposes)",
                symbol, direction, score, penalised, penalty,
            )
            self._stats["dxy_oppositions"] = self._stats.get("dxy_oppositions", 0) + 1
            score = penalised

        return max(0, min(int(score), 150))

    def evaluate_zone_gates(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        tp1: float,
        tp2: float,
        zone: EntryZone,
    ) -> bool:
        """Phase 2 Feature B — run the entry gate chain for a hypothetical entry.

        Runs the SAME gate pipeline :meth:`_run_gates_and_emit` uses (conviction
        shaping + EV/score/spread/alignment gates) so a pre-staged limit order is
        vetted by the same bar a market entry would face — it is an earlier
        execution of the gates, not a bypass. Returns True only when every gate
        passes. Never raises — a fault fails closed (returns False).
        """
        try:
            alignment = self._htf_alignment(symbol, direction)
            momentum = self._entry_momentum(symbol, direction)
            posture = self._entry_posture(alignment, momentum)
            long_p, short_p = self._entry_probabilities(symbol)
            base_score = int(getattr(zone, "conviction", 0) or 0)
            shaped = self._shaped_conviction(
                symbol, direction, base_score,
                self._read_market_state(symbol), self._current_session(),
            )
            if shaped is None:
                return False
            passed, _results = self._gate.validate_all(
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                tp1=tp1,
                tp2=tp2,
                score=shaped,
                current_spread_pips=self._get_spread(symbol),
                zone=zone,
                alignment=alignment,
                posture=posture,
                long_probability=long_p,
                short_probability=short_p,
                is_instrument_known=self._is_instrument_known(symbol),
                is_market_open=self._is_market_open(symbol),
                is_session_active=self._is_session_active(symbol),
                is_news_clear=self._is_news_clear(symbol),
                is_drawdown_ok=self._is_drawdown_ok(),
            )
            return bool(passed)
        except Exception:
            logger.debug("[entry-orch] evaluate_zone_gates failed for {}", symbol)
            return False

    def _log_session_context(self, symbol: str, direction: str) -> None:
        """Log the current trading session + its tuning for an entry.

        Observability read of the session classifier (brain/session_context.py):
        records the active ``TradingSession`` (ASIAN / LONDON / NY /
        LONDON_NY_OVERLAP) plus this symbol's resolved size multiplier and zone
        weight. Session-aware SIZING is applied downstream in the entry-decision
        pipeline (Phase 2 Feature C); this method only surfaces the read. No-op
        and never raises when the session context is unwired.
        """
        if self._session_context is None:
            return
        try:
            session = self._session_context.get_session()
            session_str = getattr(session, "value", session)
            size_mult = self._session_context.get_session_multiplier(symbol, session)
            zone_weight = self._session_context.get_session_zone_weight(symbol, session)
            self._stats["session_signals"] = (
                self._stats.get("session_signals", 0) + 1
            )
            logger.info(
                "[entry-orch] {} {} session signal: session={} size_mult={:.2f} "
                "zone_weight={:.2f}",
                symbol, direction, session_str, size_mult, zone_weight,
            )
        except Exception:
            logger.debug("[entry-orch] session logging failed for {}", symbol)

    def _run_gates_and_emit(
        self,
        symbol: str,
        info: dict,
        m1_df: pd.DataFrame,
    ) -> None:
        """After M1 confirmation, run all gates and emit entry if passed."""
        zone = info["zone"]
        direction = info["direction"]
        entry_price = info["touch_price"]
        pip_size = self._pip_size(symbol)

        # ── Market-state + session reads (Phase 2 Feature C — ACTIVE) ────
        # The compression detector's MarketState now actively shapes the entry:
        # COMPRESSING / EXPANDING boost the zone conviction (below) and, together
        # with the trading session, drive the Asian-compression caution gate. The
        # session classifier read is also logged for observability; session-aware
        # SIZING is applied downstream in the entry-decision pipeline. Both reads
        # are fully guarded so an unwired/faulty detector degrades to neutral.
        market_state = self._read_market_state(symbol)
        session = self._current_session()
        if market_state:
            self._stats["market_state_signals"] = (
                self._stats.get("market_state_signals", 0) + 1
            )
            logger.info(
                "[entry-orch] {} {} market-state={} session={} — active conviction shaping",
                symbol, direction, market_state, session or "?",
            )
        self._log_session_context(symbol, direction)

        # ── A1: opportunistic direction verification ─────────────────────
        # The zone's direction is mechanical (a bullish FVG ⇒ LONG). Before
        # committing, verify it against live momentum: the MARKET decides the
        # side, not the zone's historical kind. When the (lagging) blended
        # momentum opposes the zone direction, one of three things happens:
        # FLIP to trade WITH the move (when live tick_momentum confirms the
        # opposite AND the M5 trend does not oppose it); PROCEED on the original
        # direction (when live tick momentum AND the M5 trend BOTH agree with
        # the zone — the real-time signals lead the lagging blend for scalps);
        # or SKIP when neither holds.
        oppose_floor = -float(
            getattr(self._config, "momentum_oppose_threshold", 0.50) or 0.50
        )
        momentum = self._entry_momentum(symbol, direction)
        if momentum is not None and momentum <= oppose_floor:
            opposite = "SHORT" if str(direction).upper() == "LONG" else "LONG"
            confirmed, tick_mom, m5_trend = self._tick_m5_confirms_flip(
                opposite, symbol,
            )
            if confirmed:
                logger.info(
                    "[entry-orch] {} direction FLIP {}→{} — momentum {:+.2f} "
                    "opposed the zone, tick_mom={:+.2f} M5={}",
                    symbol, direction, opposite, momentum, tick_mom, m5_trend,
                )
                self._stats["direction_flips"] = self._stats.get("direction_flips", 0) + 1
                direction = opposite
                zone = self._flip_zone(zone, opposite, entry_price)
                info = {**info, "direction": opposite, "zone": zone}
            else:
                # The blended momentum score folds in HTF momentum (RSI+MACD on
                # M5/H1), which LAGS. For scalping, the leading signals are live
                # tick momentum and the M5 structural trend. If ticks agree with
                # the zone direction and M5 is not actively opposing (it matches
                # OR is RANGING/UNKNOWN), take the entry despite the negative
                # blended score rather than skipping a setup that current price
                # action actively supports.
                agree, tick_dir_mom, m5_dir = self._tick_m5_agree_direction(
                    direction, symbol,
                )
                if agree:
                    logger.warning(
                        "[entry-orch] {} {} entry PROCEEDS despite blended "
                        "momentum {:+.2f} opposing — live tick_mom={:+.2f} agrees "
                        "and M5={} is non-opposing (HTF momentum lags; tick+M5 "
                        "lead for scalps)",
                        symbol, direction, momentum, tick_dir_mom, m5_dir,
                    )
                    self._stats["momentum_overrides"] = (
                        self._stats.get("momentum_overrides", 0) + 1
                    )
                else:
                    logger.info(
                        "[entry-orch] {} {} entry SKIPPED — momentum {:+.2f} opposes "
                        "the zone and the flip is unconfirmed (tick_mom={:+.2f} M5={})",
                        symbol, direction, momentum, tick_mom, m5_trend,
                    )
                    self._stats["momentum_skips"] = self._stats.get("momentum_skips", 0) + 1
                    self._tick_detector.cancel_pending(symbol, "momentum opposes zone")
                    with self._lock:
                        self._confirming.pop(symbol, None)
                    return

        if pip_size <= 0:
            logger.warning(
                "[entry-orch] {} pip_size is {} — cannot compute risk, skipping",
                symbol, pip_size,
            )
            self._tick_detector.cancel_pending(symbol, "invalid pip_size")
            with self._lock:
                self._confirming.pop(symbol, None)
            return

        risk_pips = abs(entry_price - zone.invalidation_level) / pip_size
        if risk_pips <= 0:
            risk_pips = 10.0

        # SL sits at the zone's invalidation level for both directions — the
        # level where the thesis is wrong. Targets come from market structure.
        sl = zone.invalidation_level

        tp1, tp2 = self._derive_targets(symbol, direction, entry_price, sl)

        spread_pips = self._get_spread(symbol)

        alignment = self._htf_alignment(symbol, direction)
        # Phase 3 probabilistic evidence for the EV gate (direction-agnostic;
        # the gate maps these to p_win/p_loss for the trade direction).
        long_p, short_p = self._entry_probabilities(symbol)
        # Situation posture for the gate — a MIXED (no-edge) setup must show real
        # HTF support, not merely escape the permissive alignment floor.
        posture = self._entry_posture(alignment, momentum)

        # Cold-start score relaxation: while a symbol has no trade history the
        # learning layer is dormant and all gates sit at hardest defaults. We
        # temporarily credit the zone score so that trades can flow and the
        # learning layer can bootstrap. The credit shrinks linearly as trade
        # count grows and disappears entirely once MIN_TRADES is reached.
        effective_score = zone.conviction
        if self._pair_learner is not None:
            try:
                n = self._pair_learner.get_trade_count(symbol)
                min_trades = int(getattr(self._pair_learner, "MIN_TRADES", 30))
                cold_min = int(getattr(self._pair_learner, "cold_start_min_trades", 5))
                if n < cold_min:
                    # Full cold-start: apply maximum credit (lower bar by 15 pts)
                    effective_score = min(zone.conviction + 15, 123)
                    logger.debug(
                        "[cold-start] {} score {} -> {} (full cold, n={})",
                        symbol, zone.conviction, effective_score, n,
                    )
                elif n < min_trades:
                    # Graduated: credit shrinks linearly from 15 -> 0
                    frac = (n - cold_min) / max(min_trades - cold_min, 1)
                    credit = int(round(15 * (1.0 - frac)))
                    effective_score = min(zone.conviction + credit, 123)
                    logger.debug(
                        "[cold-start] {} score {} -> {} (graduated n={}/{})",
                        symbol, zone.conviction, effective_score, n, min_trades,
                    )
            except Exception as exc:
                logger.debug("[cold-start] score adjustment failed for {}: {}", symbol, exc)

        # ── Phase 2 Features C+D: conviction shaping ─────────────────────
        # Apply the compression/expansion boost, the DXY opposition penalty, and
        # the Asian-compression caution gate to the (cold-start-adjusted) score
        # before it reaches the gate. A None result means the caution gate voted
        # to skip the entry entirely.
        shaped = self._shaped_conviction(
            symbol, direction, effective_score, market_state, session,
        )
        if shaped is None:
            self._tick_detector.cancel_pending(symbol, "asian-compression caution")
            with self._lock:
                self._confirming.pop(symbol, None)
            return
        effective_score = shaped

        passed, results = self._gate.validate_all(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=sl,
            tp1=tp1,
            tp2=tp2,
            score=effective_score,
            current_spread_pips=spread_pips,
            zone=zone,
            alignment=alignment,
            posture=posture,
            long_probability=long_p,
            short_probability=short_p,
            is_instrument_known=self._is_instrument_known(symbol),
            is_market_open=self._is_market_open(symbol),
            is_session_active=self._is_session_active(symbol),
            is_news_clear=self._is_news_clear(symbol),
            is_drawdown_ok=self._is_drawdown_ok(),
        )

        # Feed the entry-gate verdict chain to the decision-trace recorder
        # (best-effort) so the event-driven system populates the dashboard's
        # decision-trace panel.  Never affects the entry flow.
        if self._on_gate_trace is not None:
            try:
                self._on_gate_trace(
                    symbol,
                    direction,
                    results,
                    passed,
                    {
                        "entry_price": entry_price,
                        "stop_loss": sl,
                        "tp1": tp1,
                        "tp2": tp2,
                        "conviction": zone.conviction,
                        "zone_type": zone.zone_type.value,
                        "timeframe": zone.timeframe,
                        "risk_pips": round(risk_pips, 2),
                        "spread_pips": round(spread_pips, 2),
                    },
                )
            except Exception:
                logger.exception("[entry-orch] gate trace callback failed")

        if passed:
            self._stats["gate_passes"] += 1
            self._stats["entries_emitted"] += 1

            # Attach the ranked Candidate that explains this zone (if any) so the
            # zone path carries the same end-to-end provenance as the consensus
            # path — best-effort; a miss leaves the legacy fields blank.
            candidate = self._match_candidate_for_zone(
                symbol, direction, zone.timeframe,
            )

            decision = {
                "symbol": symbol,
                "direction": direction,
                "entry_price": entry_price,
                "stop_loss": sl,
                "tp1": tp1,
                "tp2": tp2,
                "conviction": zone.conviction,
                "zone_type": zone.zone_type.value,
                "timeframe": zone.timeframe,
                # Entry-source attribution: this is the structural zone path
                # (zone touch → M1 confirm → gate). The consensus zoneless
                # trigger tags its own decisions source="consensus". Recorded
                # downstream so the learning loop can split win rates per path.
                "source": "zone",
                "gates_passed": [g.gate_name for g in results if g.passed],
                "risk_pips": risk_pips,
                "spread_pips": spread_pips,
                "has_sweep": zone.has_sweep,
                "is_counter_trend": getattr(zone, "is_counter_trend", False),
                "bias_direction": getattr(zone, "bias_direction", ""),
                # ── Candidate provenance (Session 2 multi-opportunity) ──────
                "candidate_id": (
                    getattr(candidate, "candidate_id", "") if candidate else ""
                ),
                "timeframe_class": (
                    getattr(candidate, "timeframe_class", "") if candidate else ""
                ),
                "candidate_score": (
                    float(getattr(candidate, "score", 0.0) or 0.0)
                    if candidate else 0.0
                ),
                "candidate_ev": (
                    float(getattr(candidate, "ev_estimate", 0.0) or 0.0)
                    if candidate else 0.0
                ),
                "contributing_modules": (
                    list(getattr(candidate, "contributing_modules", []) or [])
                    if candidate else []
                ),
                "contributing_timeframes": (
                    list(getattr(candidate, "contributing_timeframes", []) or [])
                    if candidate else []
                ),
            }

            logger.info(
                "[entry-orch] {} {} ENTRY @ {:.5f} SL={:.5f} TP1={:.5f} TP2={:.5f} score={}",
                symbol, direction, entry_price, sl, tp1, tp2, zone.conviction,
            )

            if self._on_entry is not None:
                try:
                    self._on_entry(decision)
                except Exception:
                    logger.exception("[entry-orch] on_entry callback failed")
        else:
            self._stats["gate_failures"] += 1
            failed = [g for g in results if not g.passed]
            logger.info(
                "[entry-orch] {} {} REJECTED — {}",
                symbol, direction,
                "; ".join(f"{g.gate_name}: {g.reason}" for g in failed),
            )

        # Signal the detector whether a trade was actually taken: a "filled"
        # zone stays consumed, while a rejected zone becomes eligible again once
        # price leaves and later re-approaches it (a touch alone must not
        # permanently consume a still-valid zone).
        self._tick_detector.cancel_pending(
            symbol, "filled" if passed else "rejected",
        )
        with self._lock:
            self._confirming.pop(symbol, None)
