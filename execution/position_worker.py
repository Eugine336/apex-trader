"""APEX TRADER — Position Worker (Phase 4).

Pure evaluator: given a frozen ``PositionSnapshot``, a ``Tick``, and an
optional ``WorldModel``, runs ALL management checks and returns a list of
``Intent`` objects.  **Never makes broker calls.**  The Action Executor
(Phase 6) is responsible for sending intents to the broker.

The checks mirror the two management layers in ``main_loop.py``:

1. **Tick-level** — TradeManager's update() logic: SL hit, TP1 partial,
   breakeven, trailing, TP2/TP3 full close, structure exit, stall exit.

2. **Exit checks** — invalidation, conviction collapse, HTF candle close,
   dynamic SL tightening, absolute profit protection, news exit, session
   close, spread deterioration, opportunity cost.

Thread-safe by construction: no mutable shared state, no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from execution.intents import Intent, IntentType
from execution.position_snapshot import PositionSnapshot
from config import is_always_open
from management.trailing_stop import StructureTrailingStop


def sl_within_min_room(
    current_price: float,
    new_sl: float,
    pip_size: float,
    min_room_pips: float,
) -> bool:
    """True when an SL at ``new_sl`` sits within ``min_room_pips`` of price.

    A MODIFY_SL whose level is this close to the current price cannot be placed
    by the broker (it falls inside the minimum stop distance); honouring it
    would force a clamp that pins the stop near breakeven. Callers use this to
    DEFER such moves (drop the intent) rather than emit a stop that chokes a
    still-running trade — it re-emits next cycle once price has cleared room.
    """
    if pip_size <= 0 or current_price <= 0 or not new_sl or new_sl <= 0:
        return False
    if min_room_pips <= 0:
        return False
    return abs(current_price - new_sl) < (min_room_pips * pip_size)


@dataclass
class WorkerConfig:
    """Configuration for PositionWorker management checks.

    Mirrors the config values used across TradeManager, ExitChecksMixin,
    and RiskHeatMarginMixin.  Callers build this from ``AppConfig`` once
    per cycle.
    """

    # ── Management authority (Constitution Part VI / X) ───────────────
    # True (default) → the worker runs the FULL deterministic management suite
    # (TP / breakeven / trailing / stall / invalidation / conviction / HTF /
    # dynamic-SL / opportunity-cost). False → the worker runs ONLY the always-on
    # catastrophic safety floor (hard stop-loss, weekend / session / spread
    # protection, absolute-profit backstop) and DEFERS every discretionary exit
    # to the AI Cognitive Brain, so a trade is held / protected / exited by
    # reasoning rather than a predefined level or timer. The broker-side
    # protective stop attached at entry remains the hard capital floor in either
    # case, so a position is never unprotected even if the Brain/provider is down.
    discretionary_exits_enabled: bool = True

    # ── V-009 (was V-03): LEGACY mechanical directional exits ──────────────────
    # (Constitution §I/§IV/§V/§XVII). The invalidation / conviction-collapse /
    # structure-loss-stall / H1-candle-against-side exits close a live position
    # from a re-derived scan direction+score or an opposing HTF candle — i.e.
    # "does the market still agree with our side?". That is a directional
    # comparison the constitution forbids as an exit authority; the correct path
    # is THESIS-based cognitive management. These four are LEGACY / MECHANICAL and
    # run ONLY in the non-cognition fallback mode (``discretionary_exits_enabled``
    # True). When this flag is False they are DEFERRED to the AI Cognitive Brain,
    # while the non-directional risk mechanics (TP / breakeven / trailing /
    # dynamic-SL / opportunity-cost) and the always-on catastrophic safety floor
    # (hard SL + weekend / session / spread + absolute-profit backstop, plus the
    # time-based stall fallback) remain. Default True ⇒ behaviour unchanged; every
    # firing is logged as a WARNING so operators can see the mechanical path is
    # active and migrate to Brain-managed (thesis-based) exits. When the Brain is
    # the live manager every discretionary exit is already deferred to it.
    scan_directional_exits_enabled: bool = True

    # ── TradeManager params ───────────────────────────────────────────
    breakeven_buffer_pips: float = 2.0
    breakeven_min_profit_r: float = 0.5
    partial_close_ratio: float = 0.5
    tp3_ladder_enabled: bool = False
    tp3_close_ratio: float = 0.5
    tp_adjust_enabled: bool = False

    # ── Trailing ─────────────────────────────────────────────────────
    heat_trail_tighten_enabled: bool = False
    heat_trail_factor_defensive: float = 0.7
    heat_trail_factor_reducing: float = 0.5
    heat_trail_factor_emergency: float = 0.5
    trailing_swing_lookback: int = 12

    # ── ATR trailing fallback ────────────────────────────────────────
    # When structure trailing finds no swing level to trail (e.g. a
    # consensus-triggered entry with no nearby structure), fall back to an
    # ATR-distance trail so every runner still gets a moving protective stop.
    # Structure trailing always takes priority; this only fires when it
    # produced nothing AND trailing is otherwise active.
    atr_trail_fallback_enabled: bool = True
    atr_trail_period: int = 14
    atr_trail_mult: float = 1.5

    # ── Stall exit ───────────────────────────────────────────────────
    # Opportunistic management: a flat, aging position is cut because the live
    # WorldModel no longer supports the thesis that opened it — NOT because a
    # fixed per-timeframe clock ran out. When ``stall_requires_structure_loss``
    # is True (default), the stall exit only fires once the live scan (built
    # from the current WorldModel bias) opposes the position direction or its
    # supporting conviction has decayed below ``stall_structure_score_floor``.
    # With no live read available, the position is held (never cut on a clock
    # alone). ``stall_min_hold_minutes`` is a safety floor so a brand-new flat
    # position is not cut before the brain has re-read the market — it is a
    # guardrail, not a horizon-classification timer.
    stall_requires_structure_loss: bool = True
    stall_structure_score_floor: int = 40
    stall_min_hold_minutes: float = 30.0
    # Legacy clock-based fallback. Only consulted when
    # ``stall_requires_structure_loss`` is False (opt-out for backtests /
    # environments without a live WorldModel feed). Retained for backward
    # compatibility; not used by the live opportunistic path.
    stall_limits: dict = field(default_factory=lambda: {
        "M1": 30, "M5": 60, "M15": 90, "H1": 180, "H4": 360,
    })
    stall_default_limit: int = 75

    # ── Structure exit ───────────────────────────────────────────────
    strategic_structure_intact_threshold: float = 0.6
    strategic_structure_max_age_seconds: float = 600.0
    structure_exit_tf_alignment_enabled: bool = False
    structure_exit_tf_alignment_defer: float = 0.5

    # ── Entry grace period ───────────────────────────────────────────
    # A freshly opened position is always marginally underwater (it pays the
    # spread on entry).  Discretionary "thesis changed" exits — invalidation,
    # conviction collapse, HTF-candle-close — must NOT fire inside this window,
    # otherwise any symbol whose live bias score sits below the invalidation
    # threshold gets force-closed on the very first management tick (0 pips,
    # spread-only loss).  Hard safety checks (SL/TP/breakeven/trailing/profit
    # protection/session/weekend/spread) are NEVER gated by this window.
    min_hold_seconds: float = 120.0

    # ── Invalidation check ───────────────────────────────────────────
    invalidation_score_threshold: int = 40
    opposing_signal_threshold: int = 75

    # ── Conviction collapse ──────────────────────────────────────────
    conviction_decline_cycles: int = 4
    conviction_decline_min_drop: int = 3
    conviction_profit_hold_pips: float = 20.0

    # ── Dynamic SL tightening ────────────────────────────────────────
    dynamic_sl_tightening_enabled: bool = True
    dynamic_sl_tighten_at_r: float = 2.0
    dynamic_sl_tighten_ratio: float = 0.5

    # ── SL-modify minimum room ───────────────────────────────────────
    # A breakeven / trailing / profit-protection SL move that lands within
    # this many pips of the CURRENT price cannot be honoured by the broker
    # (it sits inside the minimum stop distance) — the connector would clamp
    # it to a forced level, pinning the stop at ~breakeven and choking a
    # still-running trade. Such MODIFY_SL intents are dropped here so they
    # re-emit on a later cycle once price has cleared enough room. Expressed
    # in pips and scaled by the instrument pip size, so it is broker-agnostic.
    min_sl_modify_room_pips: float = 2.0

    # ── Absolute profit protection ───────────────────────────────────
    absolute_profit_protection_enabled: bool = True
    absolute_profit_pips: float = 50.0
    absolute_profit_usd: float = 100.0

    # ── Session close ────────────────────────────────────────────────
    session_close_enabled: bool = True
    index_close_buffer_minutes: int = 30
    dead_zone_management: bool = True

    # ── Weekend protection ───────────────────────────────────────────
    # Pre-weekend de-risk for session-gated (FX) and index positions. Mirrors
    # AppConfig.risk.weekend_protection_*; synthetics (24/7) are never touched.
    weekend_protection_enabled: bool = True
    weekend_protection_mode: str = "derisk"   # "close" | "derisk" | "reduce"
    weekend_close_buffer_minutes: int = 15
    friday_close_hour_utc: int = 21
    weekend_reduce_fraction: float = 0.5

    # ── Spread deterioration ─────────────────────────────────────────
    spread_monitor_enabled: bool = True
    spread_deterioration_multiplier: float = 3.0

    # ── Opportunity cost ─────────────────────────────────────────────
    opportunity_cost_exit_mode: str = "off"
    opportunity_cost_min_hold_minutes: float = 60.0
    opportunity_cost_max_pnl_pips: float = 5.0
    opportunity_cost_score_margin: int = 15

    # ── Portfolio state ──────────────────────────────────────────────
    portfolio_heat_state: str = "NORMAL"
    max_open_trades: int = 5


@dataclass(frozen=True)
class ScanContext:
    """Optional scan data passed to workers for strategic checks.

    When scan data is unavailable (e.g. between candle closes), workers
    skip scan-dependent checks gracefully.
    """

    direction: str = ""
    score: int = 0
    opposing_score_boost: int = 0


@dataclass(frozen=True)
class MarketContext:
    """Optional per-symbol market context for advanced checks."""

    typical_spread: Optional[float] = None
    current_spread: Optional[float] = None
    h1_last_closed_open: Optional[float] = None
    h1_last_closed_close: Optional[float] = None
    h1_last_closed_high: Optional[float] = None
    h1_last_closed_low: Optional[float] = None
    h1_last_closed_time: Optional[datetime] = None
    last_seen_h1_close: Optional[datetime] = None
    blocked_candidate: Optional[dict] = None
    m5_df: Optional[pd.DataFrame] = None  # noqa: F821


class PositionWorker:
    """Evaluates a single position and emits management Intents.

    Stateless per call — all state comes from the frozen ``PositionSnapshot``
    and the ``WorkerConfig``.  Safe to run in a ``ThreadPoolExecutor``.

    Usage::

        worker = PositionWorker(config)
        intents = worker.evaluate(snapshot, now)
    """

    def __init__(self, config: Optional[WorkerConfig] = None) -> None:
        self.cfg = config or WorkerConfig()
        self._structure_trailing = StructureTrailingStop(
            swing_lookback=self.cfg.trailing_swing_lookback,
        )

    @staticmethod
    def _warn_legacy_directional_exit(
        snap: PositionSnapshot, source: str, detail: str,
    ) -> None:
        """V-009 — surface that a LEGACY / MECHANICAL directional exit fired.

        These close a live campaign from a re-derived scan direction/score (or an
        opposing HTF candle) — a directional comparison ("does the market still
        agree with our side?") the constitution forbids as an exit authority. They
        run only in the non-cognition fallback (``discretionary_exits_enabled``
        True) with ``scan_directional_exits_enabled`` left on. Logged as a WARNING
        so the mechanical path is visible and can be migrated to thesis-based
        Brain management."""
        logger.warning(
            "[pos-worker] LEGACY directional exit '{}' fired for {} {} — {}; a "
            "mechanical direction/score rule, NOT thesis-based management (set "
            "scan_directional_exits_enabled=False to defer to the Cognitive Brain)",
            source, snap.symbol, snap.direction, detail,
        )

    def evaluate(
        self,
        snap: PositionSnapshot,
        now: Optional[datetime] = None,
        scan: Optional[ScanContext] = None,
        market: Optional[MarketContext] = None,
    ) -> list[Intent]:
        """Run all management checks and return a list of Intents.

        Returns an empty list if no action is needed.  Multiple intents
        can be returned (e.g. MODIFY_SL + PARTIAL_CLOSE); the Intent
        Aggregator (Phase 5) resolves conflicts.
        """
        now = now or datetime.now(timezone.utc)
        intents: list[Intent] = []

        if snap.trade_status in ("CLOSED", "STOPPED", "TIME_EXIT"):
            return intents

        # Position age — discretionary exits are suppressed inside the entry
        # grace window so a brand-new position is not cut on spread-only P&L
        # before it has any chance to develop.  Fail-open: if open_time is
        # missing/unparseable, treat the position as past grace (never starve
        # a real exit signal).
        try:
            age_seconds = (now - snap.open_time).total_seconds()
        except Exception:
            age_seconds = float("inf")
        past_grace = age_seconds >= self.cfg.min_hold_seconds

        # Management authority split (Constitution Part VI / X): the checks
        # marked "safety floor" below only ever protect capital and run ALWAYS
        # (even if the Brain/provider is unavailable). The DISCRETIONARY checks —
        # profit-taking, breakeven, trailing, stall, and thesis/conviction exits
        # — are deferred to the AI Cognitive Brain when it is the live manager
        # (``discretionary_exits_enabled`` False), so a trade is held / protected
        # / exited by reasoning rather than a fixed level or timer. The
        # broker-side protective stop attached at entry is the hard floor either way.
        discretionary = self.cfg.discretionary_exits_enabled

        # ── Layer 1: Tick-level checks (TradeManager.update logic) ────
        # Skip the synthetic stop-hit check while a worker-path SL move is in
        # flight but unconfirmed: the optimistic mutation has written the new
        # (still-unconfirmed) level into the snapshot, and a broker rejection
        # ("Invalid stops") would otherwise let this check fire a phantom
        # stop-loss CLOSE against a level the broker never accepted. The broker
        # still enforces the real stop server-side during this brief window.
        if not snap.sl_pending_confirmation:
            self._check_stop_loss(snap, intents)          # safety floor — always
        if discretionary:
            self._check_tp1(snap, intents)
            self._check_breakeven(snap, intents)
            self._check_tp3(snap, intents)
            self._check_tp2(snap, intents)
            self._check_stall(snap, now, intents, scan=scan)

        # ── Layer 2: Exit checks (ExitChecksMixin logic) ─────────────
        self._check_absolute_profit_protection(snap, intents)   # safety floor — always
        if discretionary:
            self._check_dynamic_sl_tightening(snap, intents)
        self._check_weekend_protection(snap, now, intents)      # safety floor — always

        if discretionary and scan is not None and past_grace:
            # V-03 — these read a compressed scan direction/score; defer to the
            # Brain when scan-directional exits are disabled.
            if self.cfg.scan_directional_exits_enabled:
                self._check_invalidation(snap, scan, intents)
                self._check_conviction_collapse(snap, intents)

        if market is not None:
            if discretionary:
                trailed = self._check_structure_trailing(snap, market, out=intents)
                if not trailed:
                    self._check_atr_trailing(snap, market, out=intents)
                # V-009 — the H1 candle-against-side close is a MECHANICAL
                # directional exit (it cuts a live campaign because the last HTF
                # candle printed against the held side — "does price still agree
                # with our direction?"), not thesis-based management. Gate it with
                # the other compressed direction/score exits so it too defers to
                # the Brain when scan-directional exits are disabled.
                if past_grace and self.cfg.scan_directional_exits_enabled:
                    self._check_htf_candle_close(snap, market, intents)
            self._check_spread_deterioration(snap, market, intents)  # safety floor
            self._check_session_close(snap, now, intents)            # safety floor
            if discretionary:
                self._check_opportunity_cost(snap, now, market, intents)

        return self._drop_too_close_sl_moves(snap, intents)

    def _drop_too_close_sl_moves(
        self, snap: PositionSnapshot, intents: list[Intent],
    ) -> list[Intent]:
        """Drop MODIFY_SL intents whose level is within the broker minimum room.

        A breakeven / trailing / profit-protection move that lands within
        ``min_sl_modify_room_pips`` of the current price would be clamped by the
        broker connector to a forced level (pinning the stop near breakeven and
        choking a still-running trade). Defer it instead — it re-emits next cycle
        once price has cleared enough room. CLOSE / PARTIAL_CLOSE / MODIFY_TP are
        never touched, so protective exits are unaffected.
        """
        room = self.cfg.min_sl_modify_room_pips
        if room <= 0:
            return intents
        kept: list[Intent] = []
        for intent in intents:
            if intent.intent_type == IntentType.MODIFY_SL and sl_within_min_room(
                snap.current_price, intent.new_sl or 0.0, snap.pip_size, room,
            ):
                logger.debug(
                    "[pos-worker] deferring SL move for {} — {} @ {:.5f} within "
                    "{:.1f}pip of price {:.5f} (source={})",
                    snap.symbol, intent.intent_type.name, intent.new_sl or 0.0,
                    room, snap.current_price, getattr(intent, "source", ""),
                )
                continue
            kept.append(intent)
        return kept

    # ──────────────────────────────────────────────────────────────────
    # Layer 1: Tick-level checks (mirrors TradeManager.update)
    # ──────────────────────────────────────────────────────────────────

    def _check_stop_loss(self, snap: PositionSnapshot, out: list[Intent]) -> None:
        if snap.sl <= 0:
            return
        if snap.is_long:
            hit = snap.current_price <= snap.sl
        else:
            hit = snap.current_price >= snap.sl
        if hit:
            out.append(Intent.close(
                symbol=snap.symbol,
                ticket=snap.order_id,
                source="stop_loss",
                reason=f"SL hit @ {snap.current_price:.5f} (SL={snap.sl:.5f})",
            ))

    def _check_tp1(self, snap: PositionSnapshot, out: list[Intent]) -> None:
        if snap.partial_closed or snap.tp1 <= 0:
            return
        if snap.is_long:
            hit = snap.current_price >= snap.tp1
        else:
            hit = snap.current_price <= snap.tp1
        if not hit:
            return
        # A partial close is impossible in two cases: (1) Deriv contracts are
        # atomic (full-close only at the executor), and (2) the position is
        # already at the broker minimum lot, so splitting it computes a zero
        # close volume — the executor would reject it and the doomed
        # PARTIAL_CLOSE would re-fire every cycle.
        #
        # In both cases the philosophy answer is to BANK the real TP1 win now
        # rather than gamble it down to a 2-pip breakeven scrape waiting for
        # TP2 — a position that can never be split can also never "run a
        # runner", so trailing to BE just exposes a real win to round-tripping
        # back to ~nothing if price reverses before TP2. Close the full
        # position at TP1. (Deriv atomic contracts: same logic — bank it.)
        vol_min = snap.volume_min or 0.01
        at_min_lot = snap.remaining_lots <= vol_min
        if snap.platform == "deriv" or at_min_lot:
            if snap.platform == "deriv":
                source = "tp1_deriv_close"
                why = "deriv atomic — cannot split, banking full win"
            else:
                source = "tp1_minlot_close"
                why = "min-lot, cannot split — banking full win"
            out.append(Intent.close(
                symbol=snap.symbol,
                ticket=snap.order_id,
                source=source,
                reason=f"TP1 hit @ {snap.tp1:.5f} — closing full position ({why})",
            ))
        else:
            ratio = snap.plan_partial_ratio or self.cfg.partial_close_ratio
            out.append(Intent.partial_close(
                symbol=snap.symbol,
                ticket=snap.order_id,
                fraction=ratio,
                source="tp1_partial",
                reason=f"TP1 hit @ {snap.tp1:.5f}, partial {ratio:.0%}",
            ))

    def _check_breakeven(self, snap: PositionSnapshot, out: list[Intent]) -> None:
        if not snap.partial_closed or snap.at_breakeven:
            return
        trigger_r = snap.plan_be_trigger_r or self.cfg.breakeven_min_profit_r
        if snap.pnl_r < trigger_r:
            return
        if snap.pnl_pips <= 0:
            return
        direction = "LONG" if snap.is_long else "SHORT"
        be_level = self._breakeven_level(
            snap.entry_price, direction, self.cfg.breakeven_buffer_pips, snap.pip_size,
        )
        if snap.is_long and be_level <= snap.sl:
            return
        if not snap.is_long and be_level >= snap.sl:
            return
        out.append(Intent.modify_sl(
            symbol=snap.symbol,
            ticket=snap.order_id,
            new_sl=be_level,
            source="breakeven",
            reason=f"BE activation @ {be_level:.5f} ({snap.pnl_r:.1f}R)",
        ))

    def _check_tp3(self, snap: PositionSnapshot, out: list[Intent]) -> None:
        if not self.cfg.tp3_ladder_enabled:
            return
        if snap.tp3 is None or snap.tp3 <= 0 or snap.tp3_hit or not snap.partial_closed:
            return
        if snap.is_long:
            hit = snap.current_price >= snap.tp3
        else:
            hit = snap.current_price <= snap.tp3
        if hit:
            out.append(Intent.partial_close(
                symbol=snap.symbol,
                ticket=snap.order_id,
                fraction=self.cfg.tp3_close_ratio,
                source="tp3_ladder",
                reason=f"TP3 hit @ {snap.tp3:.5f}",
            ))

    def _check_tp2(self, snap: PositionSnapshot, out: list[Intent]) -> None:
        if snap.tp2 is None or snap.tp2 <= 0:
            return
        if snap.is_long:
            hit = snap.current_price >= snap.tp2
        else:
            hit = snap.current_price <= snap.tp2
        if hit:
            out.append(Intent.close(
                symbol=snap.symbol,
                ticket=snap.order_id,
                source="tp2_target",
                reason=f"TP2 hit @ {snap.tp2:.5f}",
            ))

    def _check_stall(
        self,
        snap: PositionSnapshot,
        now: datetime,
        out: list[Intent],
        scan: Optional[ScanContext] = None,
    ) -> None:
        """Exit a flat, aging position when the live thesis is no longer present.

        Opportunistic management: the market decides when an idea is dead, not a
        fixed clock. A flat position is only cut once the live WorldModel (via
        ``scan``) no longer supports the direction that opened it — its bias has
        flipped against the trade, or the supporting conviction has decayed below
        ``stall_structure_score_floor``. With no live read available the position
        is held rather than cut on elapsed time alone.

        ``stall_min_hold_minutes`` is a safety floor (a new flat position is not
        cut before the brain re-reads the market); it is not a per-timeframe
        horizon timer. The legacy per-timeframe clock is only used when
        ``stall_requires_structure_loss`` is disabled (opt-out for backtests).
        """
        if snap.partial_closed:
            return
        stall_minutes = (now - snap.open_time).total_seconds() / 60
        risk_pips = snap.risk_pips
        flat_threshold = 0.15 * risk_pips if risk_pips > 0 else 5.0
        if abs(snap.pnl_pips) >= flat_threshold:
            return

        if self.cfg.stall_requires_structure_loss:
            # Safety floor — never cut a brand-new flat position before the
            # brain has had a chance to re-read the market for it.
            if stall_minutes < self.cfg.stall_min_hold_minutes:
                return
            # V-009 — the structure-loss test reads a compressed scan direction/
            # score ("does live structure still support our side?"); defer this
            # mechanical directional exit to the Brain when scan-directional exits
            # are disabled (the hard-SL floor + Brain management still apply).
            if not self.cfg.scan_directional_exits_enabled:
                return
            # No live WorldModel read → cannot confirm the thesis is gone, so
            # hold rather than exit on elapsed time alone.
            if scan is None:
                return
            if not self._stall_structure_lost(snap, scan):
                return
            self._warn_legacy_directional_exit(
                snap, "stall_exit",
                f"flat {stall_minutes:.0f}min and scan bias "
                f"{scan.direction or 'none'}@{scan.score} no longer supports side")
            out.append(Intent.close(
                symbol=snap.symbol,
                ticket=snap.order_id,
                source="stall_exit",
                reason=(
                    f"Stall exit — flat {stall_minutes:.0f}min and live structure "
                    f"no longer supports {snap.direction} "
                    f"(bias {scan.direction or 'none'}@{scan.score}), "
                    f"{snap.pnl_pips:.1f}pip"
                ),
            ))
            return

        # Legacy clock-based fallback (opt-out): per-timeframe stall window.
        stall_limit = self.cfg.stall_limits.get(
            snap.entry_timeframe, self.cfg.stall_default_limit,
        )
        if stall_minutes > stall_limit:
            out.append(Intent.close(
                symbol=snap.symbol,
                ticket=snap.order_id,
                source="stall_exit",
                reason=(
                    f"Stall exit — {stall_minutes:.0f}min (limit {stall_limit}), "
                    f"{snap.pnl_pips:.1f}pip"
                ),
            ))

    def _stall_structure_lost(
        self, snap: PositionSnapshot, scan: ScanContext,
    ) -> bool:
        """True when the live WorldModel no longer supports the position thesis.

        The supporting structure is considered gone when the live bias direction
        opposes the position, or when it still nominally agrees but its
        conviction has decayed below ``stall_structure_score_floor``.
        """
        want = "LONG" if snap.is_long else "SHORT"
        live_dir = str(scan.direction or "").strip().upper()
        if live_dir in ("LONG", "SHORT") and live_dir != want:
            return True
        if scan.score < self.cfg.stall_structure_score_floor:
            return True
        return False

    # ──────────────────────────────────────────────────────────────────
    # Layer 2: Exit checks (mirrors ExitChecksMixin)
    # ──────────────────────────────────────────────────────────────────

    def _check_absolute_profit_protection(
        self, snap: PositionSnapshot, out: list[Intent],
    ) -> None:
        if not self.cfg.absolute_profit_protection_enabled:
            return
        if snap.at_breakeven:
            return
        if snap.pnl_pips < self.cfg.absolute_profit_pips:
            return
        profit_usd = snap.pnl_pips * snap.pip_value_per_lot * snap.lots
        if profit_usd < self.cfg.absolute_profit_usd:
            return
        be_level = self._breakeven_level(
            snap.entry_price,
            "LONG" if snap.is_long else "SHORT",
            self.cfg.breakeven_buffer_pips,
            snap.pip_size,
        )
        is_improvement = (
            (snap.is_long and be_level > snap.sl)
            or (not snap.is_long and be_level < snap.sl)
        )
        if not is_improvement:
            return
        out.append(Intent.modify_sl(
            symbol=snap.symbol,
            ticket=snap.order_id,
            new_sl=be_level,
            source="absolute_profit_protection",
            reason=(
                f"Profit lock — +{snap.pnl_pips:.1f}pip / "
                f"${profit_usd:.2f} | SL→BE {be_level:.5f}"
            ),
        ))

    def _check_dynamic_sl_tightening(
        self, snap: PositionSnapshot, out: list[Intent],
    ) -> None:
        if not self.cfg.dynamic_sl_tightening_enabled:
            return
        if not snap.at_breakeven:
            return
        original_risk = abs(snap.entry_price - snap.sl_original)
        if original_risk < 1e-8:
            return
        if snap.is_long:
            profit_r = (snap.current_price - snap.entry_price) / original_risk
        else:
            profit_r = (snap.entry_price - snap.current_price) / original_risk
        if profit_r < self.cfg.dynamic_sl_tighten_at_r:
            return
        tighten_distance = original_risk * self.cfg.dynamic_sl_tighten_ratio
        if snap.is_long:
            new_sl = snap.current_price - tighten_distance
            if new_sl <= snap.sl:
                return
        else:
            new_sl = snap.current_price + tighten_distance
            if new_sl >= snap.sl:
                return
        new_sl = round(new_sl, 5)
        out.append(Intent.modify_sl(
            symbol=snap.symbol,
            ticket=snap.order_id,
            new_sl=new_sl,
            source="dynamic_sl_tighten",
            reason=f"Dynamic SL tighten — {snap.sl:.5f} → {new_sl:.5f} ({profit_r:.1f}R)",
        ))

    def _should_structure_trail(self, snap: PositionSnapshot) -> bool:
        if snap.plan_trail_strategy == "none":
            return False
        activation = snap.plan_trail_activation_r
        if activation is not None and snap.pnl_r < activation:
            return False
        return self._structure_trailing.should_trail(
            snap.pnl_pips, snap.at_breakeven,
        )

    def _check_structure_trailing(
        self, snap: PositionSnapshot, market: MarketContext, out: list[Intent],
    ) -> bool:
        """Trail the stop to recent M5 structure.

        Returns True when a structure-based MODIFY_SL intent was emitted, so the
        caller can decide whether to fall back to the ATR trail. Returns False
        when trailing is inactive or no structural level improved the stop.
        """
        if snap.pip_size <= 0:
            return False
        if not self._should_structure_trail(snap):
            return False
        df_m5 = getattr(market, "m5_df", None)
        if df_m5 is None or len(df_m5) < 10:
            return False
        try:
            trail_factor = 1.0
            if self.cfg.heat_trail_tighten_enabled:
                heat_state = str(getattr(self.cfg, "portfolio_heat_state", "NORMAL")).upper()
                if heat_state == "DEFENSIVE":
                    trail_factor = float(self.cfg.heat_trail_factor_defensive)
                elif heat_state == "REDUCING":
                    trail_factor = float(self.cfg.heat_trail_factor_reducing)
                elif heat_state == "EMERGENCY":
                    trail_factor = float(self.cfg.heat_trail_factor_emergency)

            buffer_pips = None
            if 0.0 < trail_factor < 1.0:
                buffer_pips = float(self._structure_trailing.buffer_pips) * trail_factor

            new_sl = self._structure_trailing.calculate_trail(
                "LONG" if snap.is_long else "SHORT",
                snap.sl,
                df_m5,
                snap.pip_size,
                buffer_pips=buffer_pips,
            )
            if new_sl is None:
                return False
            out.append(Intent.modify_sl(
                symbol=snap.symbol,
                ticket=snap.order_id,
                new_sl=new_sl,
                source="structure_trailing",
                reason=f"Structure trail SL → {new_sl:.5f}",
            ))
            return True
        except Exception as exc:
            logger.warning("[pos-worker] structure trailing failed for {}: {}", snap.order_id, exc)
            return False

    def _check_atr_trailing(
        self, snap: PositionSnapshot, market: MarketContext, out: list[Intent],
    ) -> None:
        """ATR-distance trailing fallback.

        Runs only when structure trailing produced no level (e.g. a
        consensus-triggered entry with no nearby swing structure) and trailing
        is otherwise active. Trails the stop at ``atr_trail_mult × ATR`` from the
        current price, never moving the stop backwards. Heat-aware: tightens the
        distance under elevated portfolio heat, mirroring structure trailing.
        """
        if not self.cfg.atr_trail_fallback_enabled:
            return
        if snap.pip_size <= 0:
            return
        if not self._should_structure_trail(snap):
            return
        df_m5 = getattr(market, "m5_df", None)
        if df_m5 is None or len(df_m5) < self.cfg.atr_trail_period + 1:
            return
        try:
            from brain.volatility_stop import latest_atr

            atr = latest_atr(df_m5, self.cfg.atr_trail_period)
            if atr is None or atr <= 0:
                return

            trail_factor = 1.0
            if self.cfg.heat_trail_tighten_enabled:
                heat_state = str(getattr(self.cfg, "portfolio_heat_state", "NORMAL")).upper()
                if heat_state == "DEFENSIVE":
                    trail_factor = float(self.cfg.heat_trail_factor_defensive)
                elif heat_state == "REDUCING":
                    trail_factor = float(self.cfg.heat_trail_factor_reducing)
                elif heat_state == "EMERGENCY":
                    trail_factor = float(self.cfg.heat_trail_factor_emergency)

            distance = float(self.cfg.atr_trail_mult) * atr
            if 0.0 < trail_factor < 1.0:
                distance *= trail_factor
            if distance <= 0:
                return

            if snap.is_long:
                new_sl = round(snap.current_price - distance, 5)
                if new_sl <= snap.sl:
                    return
            else:
                new_sl = round(snap.current_price + distance, 5)
                if new_sl >= snap.sl:
                    return
            out.append(Intent.modify_sl(
                symbol=snap.symbol,
                ticket=snap.order_id,
                new_sl=new_sl,
                source="atr_trailing",
                reason=f"ATR trail SL → {new_sl:.5f} ({self.cfg.atr_trail_mult:.1f}×ATR)",
            ))
        except Exception as exc:
            logger.warning("[pos-worker] ATR trailing failed for {}: {}", snap.order_id, exc)

    def _check_invalidation(
        self, snap: PositionSnapshot, scan: ScanContext, out: list[Intent],
    ) -> None:
        """V-009 LEGACY/MECHANICAL — close on a re-derived scan direction/score.

        Reads a compressed scan ``direction``/``score`` and closes the position
        when it reads low or opposes the held side. This is a directional
        comparison, not thesis-based management; it runs only in the non-cognition
        fallback and each firing is warned. The thesis-based path is the Brain."""
        if scan.score < self.cfg.invalidation_score_threshold:
            if snap.pnl_pips <= 0:
                self._warn_legacy_directional_exit(
                    snap, "invalidation_low_score",
                    f"low scan score ({scan.score}) with negative P&L")
                out.append(Intent.close(
                    symbol=snap.symbol,
                    ticket=snap.order_id,
                    source="invalidation_low_score",
                    reason=f"Low score ({scan.score}) with negative P&L",
                ))
                return

        opposing = (
            (snap.is_long and scan.direction == "SHORT")
            or (not snap.is_long and scan.direction == "LONG")
        )
        effective_score = min(100, int(scan.score + max(0, scan.opposing_score_boost)))
        if opposing and effective_score >= self.cfg.opposing_signal_threshold:
            self._warn_legacy_directional_exit(
                snap, "invalidation_opposing",
                f"opposing scan signal {scan.direction}@{effective_score}")
            out.append(Intent.close(
                symbol=snap.symbol,
                ticket=snap.order_id,
                source="invalidation_opposing",
                reason=(
                    f"Opposing signal ({scan.direction}@{effective_score}) "
                    f"vs {snap.direction}"
                ),
            ))

    def _check_conviction_collapse(
        self, snap: PositionSnapshot, out: list[Intent],
    ) -> None:
        """V-009 LEGACY/MECHANICAL — close on a declining scan-score trajectory.

        Reads the position's scan ``score_history`` and closes when the score has
        declined for N cycles. A re-derived directional/score judgement, not
        thesis-based management; runs only in the non-cognition fallback and each
        firing is warned."""
        n = self.cfg.conviction_decline_cycles
        scores = snap.score_history
        if len(scores) < n:
            return
        recent = scores[-n:]
        is_declining = all(
            recent[i] - recent[i + 1] >= self.cfg.conviction_decline_min_drop
            for i in range(len(recent) - 1)
        )
        if not is_declining:
            return
        if snap.pnl_pips > self.cfg.conviction_profit_hold_pips:
            return
        self._warn_legacy_directional_exit(
            snap, "conviction_collapse",
            f"declining scan scores {recent[0]}→{recent[-1]}")
        out.append(Intent.close(
            symbol=snap.symbol,
            ticket=snap.order_id,
            source="conviction_collapse",
            reason=f"Conviction collapse — scores {recent[0]}→{recent[-1]}",
        ))

    def _check_htf_candle_close(
        self,
        snap: PositionSnapshot,
        market: MarketContext,
        out: list[Intent],
    ) -> None:
        """V-009 LEGACY/MECHANICAL — close when the last H1 candle closes against
        the held side.

        This is a directional comparison ("did the higher timeframe just print a
        candle against our direction?"), not thesis-based management. It is gated
        by ``scan_directional_exits_enabled`` at the call site and each firing is
        warned; the thesis-based path is the Cognitive Brain."""
        if market.h1_last_closed_open is None or market.h1_last_closed_close is None:
            return
        if market.h1_last_closed_time is None:
            return
        if (
            market.last_seen_h1_close is not None
            and market.h1_last_closed_time <= market.last_seen_h1_close
        ):
            return
        candle_open = market.h1_last_closed_open
        candle_close = market.h1_last_closed_close
        if candle_open == 0 or candle_close == 0:
            return
        candle_body = abs(candle_close - candle_open)
        candle_range = (
            (market.h1_last_closed_high or candle_close)
            - (market.h1_last_closed_low or candle_open)
        )
        if candle_range > 0 and (candle_body / candle_range) < 0.3:
            return
        candle_bearish = candle_close < candle_open
        candle_bullish = candle_close > candle_open
        opposing_close = (
            (snap.is_long and candle_bearish)
            or (not snap.is_long and candle_bullish)
        )
        if not opposing_close:
            return
        if snap.pnl_pips > 30:
            return
        direction_str = "BEARISH" if candle_bearish else "BULLISH"
        self._warn_legacy_directional_exit(
            snap, "htf_candle_close",
            f"H1 {direction_str} candle closed against the held side")
        out.append(Intent.close(
            symbol=snap.symbol,
            ticket=snap.order_id,
            source="htf_candle_close",
            reason=f"H1 {direction_str} candle close against {snap.direction}",
        ))

    def _check_spread_deterioration(
        self,
        snap: PositionSnapshot,
        market: MarketContext,
        out: list[Intent],
    ) -> None:
        if not self.cfg.spread_monitor_enabled:
            return
        if market.typical_spread is None or market.typical_spread < 1e-8:
            return
        if market.current_spread is None:
            return
        spread_ratio = market.current_spread / market.typical_spread
        if spread_ratio < self.cfg.spread_deterioration_multiplier:
            return
        if snap.at_breakeven:
            return
        be_level = self._breakeven_level(
            snap.entry_price,
            "LONG" if snap.is_long else "SHORT",
            self.cfg.breakeven_buffer_pips,
            snap.pip_size,
        )
        is_improvement = (
            (snap.is_long and be_level > snap.sl)
            or (not snap.is_long and be_level < snap.sl)
        )
        if not is_improvement:
            return
        out.append(Intent.modify_sl(
            symbol=snap.symbol,
            ticket=snap.order_id,
            new_sl=be_level,
            source="spread_deterioration",
            reason=f"Spread {spread_ratio:.1f}× normal — SL→BE {be_level:.5f}",
        ))

    def _check_session_close(
        self,
        snap: PositionSnapshot,
        now: datetime,
        out: list[Intent],
    ) -> None:
        if not self.cfg.session_close_enabled:
            return
        utc_hour = now.hour
        utc_minute = now.minute
        index_close_windows = {
            "HK50": (8, 0),
            "JP225": (6, 30),
            "AUS200": (6, 0),
            "GER40": (20, 0),
            "FRA40": (20, 0),
            "UK100": (16, 30),
            "US30": (21, 0),
            "US100": (21, 0),
            "US500": (21, 0),
        }
        close_time = index_close_windows.get(snap.symbol)
        if close_time:
            close_h, close_m = close_time
            close_total = close_h * 60 + close_m
            now_total = utc_hour * 60 + utc_minute
            diff = close_total - now_total
            if 0 < diff <= self.cfg.index_close_buffer_minutes:
                out.append(Intent.close(
                    symbol=snap.symbol,
                    ticket=snap.order_id,
                    source="session_close",
                    reason=f"Exchange closes in {diff}min",
                ))
                return

        if self.cfg.dead_zone_management:
            if "USD" in snap.symbol or "JPY" in snap.symbol:
                in_dead_zone = utc_hour == 0 or (utc_hour == 1 and utc_minute <= 59)
                if in_dead_zone and not snap.at_breakeven:
                    be_level = self._breakeven_level(
                        snap.entry_price,
                        "LONG" if snap.is_long else "SHORT",
                        self.cfg.breakeven_buffer_pips,
                        snap.pip_size,
                    )
                    if self._be_move_is_valid(snap, be_level):
                        out.append(Intent.modify_sl(
                            symbol=snap.symbol,
                            ticket=snap.order_id,
                            new_sl=be_level,
                            source="dead_zone_protection",
                            reason=f"Dead zone — SL→BE {be_level:.5f}",
                        ))
                    else:
                        logger.debug(
                            "[dead-zone] {} BE move skipped — not profitable enough "
                            "or SL would not improve (pnl={:.1f}p sl={:.5f} be={:.5f})",
                            snap.symbol, snap.pnl_pips, snap.sl, be_level,
                        )

    def _check_weekend_protection(
        self,
        snap: PositionSnapshot,
        now: datetime,
        out: list[Intent],
    ) -> None:
        """Pre-weekend protection for session-gated (FX) and index positions.

        Within ``weekend_close_buffer_minutes`` of the Friday FX close, apply
        the configured action so positions are not carried over the weekend
        gap unmanaged.  24/7 synthetics are never touched.

        Modes:
            "close"  — flatten the position
            "derisk" — move the SL to breakeven (if not already there)
            "reduce" — partially close to cut weekend exposure
        """
        if not self.cfg.weekend_protection_enabled:
            return
        # Never touch 24/7 synthetics — they trade through the weekend.
        if is_always_open(snap.symbol):
            return
        if now.weekday() != 4:  # Friday only
            return
        close_total = self.cfg.friday_close_hour_utc * 60
        now_total = now.hour * 60 + now.minute
        diff = close_total - now_total
        if not (0 < diff <= self.cfg.weekend_close_buffer_minutes):
            return

        mode = self.cfg.weekend_protection_mode
        if mode == "close":
            out.append(Intent.close(
                symbol=snap.symbol,
                ticket=snap.order_id,
                source="weekend_protection",
                reason=f"Weekend protection — FX closes in {diff}min (close)",
            ))
        elif mode == "reduce":
            if not snap.partial_closed:
                out.append(Intent.partial_close(
                    symbol=snap.symbol,
                    ticket=snap.order_id,
                    fraction=self.cfg.weekend_reduce_fraction,
                    source="weekend_protection",
                    reason=f"Weekend protection — FX closes in {diff}min (reduce)",
                ))
        else:  # "derisk" — move SL to breakeven
            if not snap.at_breakeven and snap.pip_size > 0:
                be_level = self._breakeven_level(
                    snap.entry_price,
                    "LONG" if snap.is_long else "SHORT",
                    self.cfg.breakeven_buffer_pips,
                    snap.pip_size,
                )
                if self._be_move_is_valid(snap, be_level):
                    out.append(Intent.modify_sl(
                        symbol=snap.symbol,
                        ticket=snap.order_id,
                        new_sl=be_level,
                        source="weekend_protection",
                        reason=f"Weekend protection — FX closes in {diff}min (SL→BE {be_level:.5f})",
                    ))
                else:
                    logger.debug(
                        "[weekend] {} BE derisk skipped — not profitable enough "
                        "or SL would not improve (pnl={:.1f}p sl={:.5f} be={:.5f})",
                        snap.symbol, snap.pnl_pips, snap.sl, be_level,
                    )

    def _check_opportunity_cost(
        self,
        snap: PositionSnapshot,
        now: datetime,
        market: MarketContext,
        out: list[Intent],
    ) -> None:
        if self.cfg.opportunity_cost_exit_mode == "off":
            return
        if market.blocked_candidate is None:
            return
        hold_minutes = (now - snap.open_time).total_seconds() / 60
        if hold_minutes < self.cfg.opportunity_cost_min_hold_minutes:
            return
        if snap.pnl_pips > self.cfg.opportunity_cost_max_pnl_pips:
            return
        if snap.partial_closed:
            return
        blocked = market.blocked_candidate
        score_delta = blocked.get("score", 0) - snap.score
        if score_delta < self.cfg.opportunity_cost_score_margin:
            return
        if self.cfg.opportunity_cost_exit_mode == "active":
            out.append(Intent.close(
                symbol=snap.symbol,
                ticket=snap.order_id,
                source="opportunity_cost",
                reason=(
                    f"Opportunity cost — blocked {blocked.get('pair', '?')} "
                    f"(score={blocked.get('score', 0)}, delta=+{score_delta})"
                ),
            ))

    # ──────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _breakeven_level(
        entry_price: float,
        direction: str,
        buffer_pips: float,
        pip_size: float,
    ) -> float:
        if direction == "LONG":
            return round(entry_price + buffer_pips * pip_size, 5)
        return round(entry_price - buffer_pips * pip_size, 5)

    @staticmethod
    def _be_move_is_valid(snap: PositionSnapshot, be_level: float) -> bool:
        """Return True only if moving SL to ``be_level`` is safe to submit.

        Guards the dead-zone / weekend breakeven moves so they never emit an
        SL that the broker would reject as "Invalid stops":

        1. The trade must be profitable enough (> 2 pips) — a flat/new
           position has no profit to protect and BE sits the wrong side of price.
        2. The proposed SL must IMPROVE the current SL (move it closer to
           price for a winner), never loosen it. For a long, ``be_level`` must
           be above the current SL; for a short, below it.
        """
        if snap.pnl_pips <= 2.0:
            return False
        if snap.is_long:
            return be_level > snap.sl
        return be_level < snap.sl
