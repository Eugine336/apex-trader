"""
APEX TRADER — Adaptive Optimizer
The master learner. Coordinates all learning modules,
runs optimisation cycles, and feeds adjustments back
into the trading system. Gets smarter every day.

Every trade teaches me something. I remember what works
on each pair, each session, each regime. I adapt. I evolve.
What worked last month might not work this month — I know
the difference.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from loguru import logger

from adaptive.trade_analyzer import PerformanceProfile, TradeAnalyzer
from adaptive.score_optimizer import ScoreOptimizer, ScoringWeights
from adaptive.regime_learner import RegimeLearner, RegimeStrategy
from adaptive.pair_learner import PairLearner, PairProfile
from adaptive.session_learner import SessionLearner, SessionProfile
from adaptive.tunable import TuningGuardMixin


@dataclass
class TradeAdjustments:
    score_threshold_adjustment: int = 0
    position_size_multiplier: float = 1.0
    tp_multiplier: float = 1.0
    sl_buffer_adjustment: float = 0.0
    should_trade: bool = True
    confidence: float = 0.0
    reason: str = "defaults"


@dataclass
class OptimizationReport:
    new_scoring_weights: ScoringWeights = field(default_factory=ScoringWeights)
    regime_strategies: dict[str, RegimeStrategy] = field(default_factory=dict)
    pair_profiles: dict[str, PairProfile] = field(default_factory=dict)
    session_profiles: dict[str, SessionProfile] = field(default_factory=dict)
    overall_performance: Optional[PerformanceProfile] = None
    recommendations: list[str] = field(default_factory=list)
    trades_analyzed: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AdaptiveOptimizer(TuningGuardMixin):
    """
    APEX TRADER — Adaptive Optimizer
    Coordinates all learning modules, runs optimisation cycles,
    and feeds adjustments back into the trading system.
    """

    RETRAIN_TRADE_INTERVAL = 50
    RETRAIN_DAY_INTERVAL = 7

    # ── Event-driven retrain (verification gap #3) ────────────────────────
    # The periodic triggers above are batch: between cycles the system runs on
    # stale learned parameters even when the market has clearly shifted. An
    # event-driven trigger lets a regime change, a drawdown-mode escalation, or
    # a loss streak force an immediate retrain on the next check — so learning
    # stops being "every 50 trades / 7 days" and reacts to material change. A
    # cooldown prevents thrashing (back-to-back retrains on a noisy signal).
    EVENT_RETRAIN_ENABLED = True
    EVENT_RETRAIN_COOLDOWN_MINUTES = 60
    LOSS_STREAK_RETRAIN_THRESHOLD = 3

    # Recency: the learners adapt to the CURRENT regime by training on trades
    # within this rolling window — but only if enough remain (≥ min trades),
    # otherwise the full history is used so a young account never starves the
    # learners. 0 days disables windowing (full history, legacy behaviour).
    RECENCY_WINDOW_DAYS = 90
    RECENCY_MIN_TRADES = 50
    # When the window falls back to the full history, trades older than the
    # window are exponentially down-weighted with this half-life so stale,
    # months-old regime data fades instead of counting equally forever.
    RECENCY_HALF_LIFE_DAYS = 60.0

    # Losing-pattern hard-block thresholds. A cached multi-dimensional
    # combination (pair×session, pair×regime, …) only becomes a live entry
    # block when it has at least this many samples AND a win rate at or below
    # the ceiling — deliberately stricter than the analyzer's display threshold
    # (win_rate < 0.45, n ≥ 10) so we never block on a marginal/under-sampled
    # pattern.
    LOSING_PATTERN_MIN_SAMPLES = 20
    LOSING_PATTERN_MAX_WIN_RATE = 0.35

    def __init__(self, config=None) -> None:
        self.analyzer = TradeAnalyzer()
        self.optimizer = ScoreOptimizer(config=getattr(config, "scoring", None))
        self.regime_learner = RegimeLearner(config=getattr(config, "regime_learner", None))
        self.pair_learner = PairLearner(config=getattr(config, "pair_learner", None))
        self.session_learner = SessionLearner(config=getattr(config, "session_learner", None))

        # Tunable per instance (kept as attributes so ops can adjust/disable).
        # Read from config with the class constants as defaults so a year-long
        # deployment can retune the recency policy without a code change.
        self.recency_window_days = int(
            getattr(config, "recency_window_days", self.RECENCY_WINDOW_DAYS)
            if config is not None else self.RECENCY_WINDOW_DAYS
        )
        self.recency_min_trades = int(
            getattr(config, "recency_min_trades", self.RECENCY_MIN_TRADES)
            if config is not None else self.RECENCY_MIN_TRADES
        )
        self.recency_half_life_days = float(
            getattr(config, "recency_half_life_days", self.RECENCY_HALF_LIFE_DAYS)
            if config is not None else self.RECENCY_HALF_LIFE_DAYS
        )

        # Losing-pattern gate: tunable per instance; the cached patterns are
        # refreshed on every optimisation pass.
        self.losing_pattern_min_samples = self.LOSING_PATTERN_MIN_SAMPLES
        self.losing_pattern_max_win_rate = self.LOSING_PATTERN_MAX_WIN_RATE
        self._losing_patterns: list[dict] = []

        self._last_train_time: Optional[datetime] = None
        self._trades_since_train: int = 0
        self._last_recommendations: list[str] = []

        # Event-driven retrain state (gap #3). Tunable per instance; optional
        # overrides may be supplied on the config object (any attribute access
        # is guarded so a config without these fields keeps the defaults).
        self.event_retrain_enabled = bool(
            getattr(config, "event_retrain_enabled", self.EVENT_RETRAIN_ENABLED)
        )
        self.event_retrain_cooldown_minutes = int(
            getattr(
                config,
                "event_retrain_cooldown_minutes",
                self.EVENT_RETRAIN_COOLDOWN_MINUTES,
            )
        )
        self.loss_streak_retrain_threshold = int(
            getattr(
                config,
                "loss_streak_retrain_threshold",
                self.LOSS_STREAK_RETRAIN_THRESHOLD,
            )
        )
        self._event_retrain_pending: bool = False
        self._event_retrain_reason: str = ""
        self._last_event_retrain_time: Optional[datetime] = None

        # Closed-trade history provider.  Wired at startup to the persistent
        # TradeJournal (see EventDrivenSystem.start) so the learners train on
        # the real recorded outcomes.  Without it, get_trade_history() returns
        # an empty list and every learner trains on nothing.
        self._trade_history_provider: Optional[Callable[[], list[dict]]] = None

    def set_trade_history_provider(
        self, provider: Callable[[], list[dict]]
    ) -> None:
        """Inject the closed-trade source the learners train on.

        ``provider`` is a zero-arg callable returning the full closed-trade
        history as a list of plain dicts (e.g. TradeJournal.get_all_trades_as_dicts).
        """
        self._trade_history_provider = provider

    def get_trade_history(self) -> list[dict]:
        """Return the recorded closed-trade history for learner training.

        Returns an empty list (never raises) when no provider is wired or the
        provider fails, so a degraded history source can never break the tune
        cycle — it just means the learners have no fresh data this pass.
        """
        if self._trade_history_provider is None:
            return []
        try:
            trades = self._trade_history_provider()
            return list(trades) if trades else []
        except Exception as exc:
            logger.warning(
                "[ml-adapter] trade-history provider failed: {} — {}",
                type(exc).__name__, exc,
            )
            return []

    def run_optimization(self, trades: list[dict]) -> OptimizationReport:
        # When the Tuner Agent is sole authority it drives the sub-learners
        # individually; a direct run_optimization() is a bypass — return an
        # inert report instead of re-tuning behind the agent's back.
        if self._tuning_blocked("run_optimization"):
            return OptimizationReport(trades_analyzed=len(trades))
        n = len(trades)
        logger.info(f"Adaptive optimisation cycle — {n} trades")

        # Train the learners on RECENT trades so they reflect the current regime
        # (the full history still drives the all-time performance report below).
        learner_trades = self._recent_trades(
            trades, self.recency_window_days, self.recency_min_trades,
            half_life_days=self.recency_half_life_days,
        )
        if len(learner_trades) < n:
            logger.info(
                "Recency window — learners train on last {}d: {}/{} trades",
                self.recency_window_days, len(learner_trades), n,
            )

        overall = self.analyzer.analyze_all(trades)
        weights = self.optimizer.optimize(learner_trades)
        regimes = self.regime_learner.learn(learner_trades)
        pairs = self.pair_learner.learn(learner_trades)
        sessions = self.session_learner.learn(learner_trades)

        # Cache statistically-confident losing combinations for the live
        # defensive entry gate (is_losing_pattern). Built from the recency
        # window so the block reflects the CURRENT regime, like the learners.
        self._losing_patterns = self.analyzer.get_losing_patterns(learner_trades)

        recommendations = self._generate_recommendations(
            trades, overall, regimes, pairs, sessions
        )

        self._last_train_time = datetime.now(timezone.utc)
        self._trades_since_train = 0

        report = OptimizationReport(
            new_scoring_weights=weights,
            regime_strategies=regimes,
            pair_profiles=pairs,
            session_profiles=sessions,
            overall_performance=overall,
            recommendations=recommendations,
            trades_analyzed=n,
        )
        logger.info(f"Optimisation complete — {len(recommendations)} recommendations")
        return report

    @staticmethod
    def _parse_trade_ts(t: dict) -> Optional[datetime]:
        """Best-effort UTC timestamp for a trade dict (timestamp/entry/close)."""
        raw = t.get("timestamp") or t.get("entry_time") or t.get("close_time")
        if not raw:
            return None
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _recent_trades(
        cls,
        trades: list[dict],
        window_days: int,
        min_trades: int,
        now: Optional[datetime] = None,
        half_life_days: float = RECENCY_HALF_LIFE_DAYS,
    ) -> list[dict]:
        """Return trades within `window_days` — but only if at least `min_trades`
        remain; otherwise return the full set so the learners never starve. This
        is what lets the learners adapt to the current regime instead of being
        anchored by stale, months-old trades. Disabled when window_days <= 0.

        When it falls back to the full history (not enough recent trades), each
        returned trade is tagged with a ``_recency_weight`` so older trades decay
        exponentially (half-life ``half_life_days``) instead of counting equally:
        trades inside the window keep weight 1.0; older ones fade. Learners that
        read the field use weighted stats; those that don't are unaffected."""
        if window_days <= 0:
            # Windowing disabled — legacy behaviour, no weighting.
            return trades
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=window_days)
        if len(trades) > min_trades:
            recent = [
                t for t in trades
                if (ts := cls._parse_trade_ts(t)) is not None and ts >= cutoff
            ]
            if len(recent) >= min_trades:
                # Enough fresh trades — train on the window itself (all weight 1.0).
                return recent
        # Fallback: full history, but time-decayed so ancient regime data fades.
        return cls._apply_recency_weights(trades, window_days, half_life_days, now)

    @classmethod
    def _apply_recency_weights(
        cls,
        trades: list[dict],
        window_days: int,
        half_life_days: float,
        now: datetime,
    ) -> list[dict]:
        """Return shallow copies of `trades` carrying a ``_recency_weight``.

        Trades inside the recency window get 1.0; older trades decay as
        ``exp(-ln(2)/half_life_days * days_old)``. Trades with an unreadable
        timestamp default to 1.0 (neutral — never zeroed out). Input dicts are
        never mutated."""
        import math

        from adaptive.recency_weight import RECENCY_WEIGHT_KEY

        lam = (math.log(2.0) / half_life_days) if half_life_days > 0 else 0.0
        weighted: list[dict] = []
        for t in trades:
            ts = cls._parse_trade_ts(t)
            if ts is None:
                weight = 1.0
            else:
                days_old = max(0.0, (now - ts).total_seconds() / 86400.0)
                if days_old <= window_days:
                    weight = 1.0
                else:
                    weight = math.exp(-lam * days_old)
                    if not math.isfinite(weight):
                        weight = 0.0
            copy = dict(t)
            copy[RECENCY_WEIGHT_KEY] = round(weight, 6)
            weighted.append(copy)
        return weighted

    def get_trade_adjustments(
        self, pair: str, regime: str, session: str
    ) -> TradeAdjustments:
        # Pass the symbol so the regime/session learners can consult their
        # per-symbol (tier 4) profiles when those buckets have enough data;
        # they silently fall back to the global profiles otherwise.
        regime_strat = self.regime_learner.get_strategy(regime, symbol=pair)
        pair_mult = self.pair_learner.get_pair_multiplier(pair)
        session_agg = self.session_learner.get_session_aggression(session, symbol=pair)

        can_trade_regime, regime_reason = self.regime_learner.should_trade_regime(
            regime, symbol=pair
        )
        if not can_trade_regime:
            return TradeAdjustments(
                should_trade=False, confidence=regime_strat.confidence, reason=regime_reason
            )
        if pair_mult == 0.0:
            return TradeAdjustments(
                should_trade=False, confidence=0.8, reason=f"{pair} — AVOID recommendation"
            )

        threshold_adj = regime_strat.optimal_score_threshold - 85
        tp_mult = regime_strat.optimal_tp_multiplier
        sl_adj = regime_strat.optimal_sl_buffer_pips - 2.0
        size_mult = pair_mult

        if session_agg == "AGGRESSIVE":
            size_mult = min(size_mult * 1.1, 1.2)
        elif session_agg == "CAUTIOUS":
            size_mult *= 0.85
        elif session_agg == "AVOID":
            return TradeAdjustments(
                should_trade=False,
                confidence=0.6,
                reason=f"Session {session} has poor historical performance — avoiding",
            )

        confidence = (regime_strat.confidence + min(1.0, pair_mult)) / 2.0
        parts = [f"regime={regime}", f"pair_mult={pair_mult:.1f}", f"session={session_agg}"]
        reason = " | ".join(parts)

        return TradeAdjustments(
            score_threshold_adjustment=threshold_adj,
            position_size_multiplier=round(size_mult, 3),
            tp_multiplier=round(tp_mult, 2),
            sl_buffer_adjustment=round(sl_adj, 2),
            should_trade=True,
            confidence=round(confidence, 4),
            reason=reason,
        )

    def get_recommendations(self) -> list[str]:
        return self._last_recommendations[:]

    def is_losing_pattern(
        self, pair: str, regime: str, session: str, entry_type: str = ""
    ) -> tuple[bool, str]:
        """Defensive entry check: does this setup match a statistically-confident
        losing multi-dimensional pattern from our own trade history?

        Only blocks when a cached pattern (refreshed each optimisation pass via
        ``get_losing_patterns``) has at least ``losing_pattern_min_samples`` trades
        AND a win rate at or below ``losing_pattern_max_win_rate``. Neutral (never
        blocks) until enough history accumulates. Returns ``(is_loser, reason)``.
        """
        setup = {
            "pair": str(pair),
            "regime": str(regime),
            "session": str(session),
            "entry_type": str(entry_type),
        }
        for pat in self._losing_patterns:
            if pat.get("sample_size", 0) < self.losing_pattern_min_samples:
                continue
            if pat.get("win_rate", 1.0) > self.losing_pattern_max_win_rate:
                continue
            dims = pat.get("dimensions", {})
            if not dims:
                continue
            if all(setup.get(k, "") == str(v) for k, v in dims.items()):
                label = ", ".join(f"{k}={v}" for k, v in dims.items())
                return True, (
                    f"{label} — {pat['win_rate']:.0%} win rate over "
                    f"{pat['sample_size']} trades"
                )
        return False, ""

    def should_retrain(
        self, last_train_time: Optional[datetime] = None, new_trades_since: int = 0
    ) -> bool:
        ltt = last_train_time or self._last_train_time
        nts = new_trades_since or self._trades_since_train

        # Event-driven trigger takes priority over the periodic checks: a regime
        # shift / drawdown escalation / loss streak fires an immediate retrain on
        # the next check, then clears the flag (one retrain per event).
        if self._event_retrain_pending:
            self._event_retrain_pending = False
            logger.info(
                "[ml] event-driven retrain triggered: {}",
                self._event_retrain_reason or "unspecified",
            )
            return True

        if ltt is None:
            return True
        if nts >= self.RETRAIN_TRADE_INTERVAL:
            return True
        elapsed = (datetime.now(timezone.utc) - ltt).days
        return elapsed >= self.RETRAIN_DAY_INTERVAL

    # ------------------------------------------------------------------
    # Event-driven retrain triggers (gap #3)
    # ------------------------------------------------------------------
    # These notification hooks let other subsystems force an out-of-band
    # retrain when the market materially changes, instead of waiting for the
    # next periodic cycle. They only ARM the flag (checked in should_retrain);
    # the actual retrain is still owned by the optimisation loop. Callers are
    # wired separately (see the integration notes on each method).

    def trigger_event_retrain(self, reason: str) -> bool:
        """Arm an out-of-band retrain on the next ``should_retrain`` check.

        Respects ``event_retrain_enabled`` and the cooldown so a noisy signal
        cannot thrash the learners. Returns True when the flag was armed, False
        when suppressed (disabled or within cooldown). Never raises.
        """
        try:
            if not self.event_retrain_enabled:
                return False
            now = datetime.now(timezone.utc)
            cooldown = timedelta(minutes=max(0, self.event_retrain_cooldown_minutes))
            if (
                self._last_event_retrain_time is not None
                and now - self._last_event_retrain_time < cooldown
            ):
                logger.debug(
                    "[ml] event retrain '{}' suppressed — within {}min cooldown",
                    reason, self.event_retrain_cooldown_minutes,
                )
                return False
            self._event_retrain_pending = True
            self._event_retrain_reason = reason
            self._last_event_retrain_time = now
            logger.info("[ml] event retrain armed: {}", reason)
            return True
        except Exception as exc:  # noqa: BLE001 — never break a caller hot path
            logger.debug("[ml] trigger_event_retrain failed: {}", exc)
            return False

    def notify_loss_streak(self, consecutive_losses: int) -> bool:
        """Arm a retrain when a loss streak reaches the configured threshold.

        # Caller integration: invoke from the trade-close path (e.g.
        # EventDrivenSystem._on_trade_closed) with the running consecutive-loss
        # count once that count is tracked.
        """
        try:
            if int(consecutive_losses) >= self.loss_streak_retrain_threshold:
                return self.trigger_event_retrain(
                    f"loss_streak={int(consecutive_losses)}"
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ml] notify_loss_streak failed: {}", exc)
        return False

    def notify_drawdown_escalation(self, old_mode: str, new_mode: str) -> bool:
        """Arm a retrain when the drawdown guard escalates to a tighter mode.

        # Caller integration: invoke from DrawdownGuard's mode-transition path
        # when the mode moves to a MORE defensive state (e.g. NORMAL→CAUTION,
        # CAUTION→RECOVERY, NORMAL→RECOVERY).
        """
        try:
            severity = {"NORMAL": 0, "CAUTION": 1, "RECOVERY": 2, "FROZEN": 3}
            old_s = severity.get(str(old_mode).upper(), 0)
            new_s = severity.get(str(new_mode).upper(), 0)
            if new_s > old_s:
                return self.trigger_event_retrain(
                    f"drawdown_escalation={old_mode}->{new_mode}"
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ml] notify_drawdown_escalation failed: {}", exc)
        return False

    def notify_regime_change(self, old_regime: str, new_regime: str) -> bool:
        """Arm a retrain when the detected market regime changes.

        # Caller integration: invoke from RegimeDetector when the classified
        # regime transitions to a different label.
        """
        try:
            if str(old_regime) != str(new_regime) and str(new_regime):
                return self.trigger_event_retrain(
                    f"regime_change={old_regime}->{new_regime}"
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ml] notify_regime_change failed: {}", exc)
        return False

    def register_new_trade(self, exit_cause: Optional[str] = None) -> None:
        self._trades_since_train += 1
        # P7: the normalised exit cause is an additive learning signal. The
        # retrain trigger itself is count-based, so we only surface the cause
        # here (kwarg is optional — existing callers are unaffected).
        if exit_cause:
            logger.debug("[ml] trade registered — exit_cause={}", exit_cause)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _generate_recommendations(
        self,
        trades: list[dict],
        overall: PerformanceProfile,
        regimes: dict[str, RegimeStrategy],
        pairs: dict[str, PairProfile],
        sessions: dict[str, SessionProfile],
    ) -> list[str]:
        recs: list[str] = []

        if overall.total_trades > 0:
            recs.append(
                f"Overall: {overall.win_rate:.0%} win rate, "
                f"PF={overall.profit_factor:.2f}, "
                f"expectancy={overall.expectancy:.2f}"
            )

        effectiveness = self.optimizer.get_factor_effectiveness(trades)
        for factor, stats in effectiveness.items():
            if stats["sample_present"] >= 10 and stats["lift"] > 0.10:
                recs.append(
                    f"Increase weight on {factor} — "
                    f"{stats['win_rate_when_present']:.0%} when present vs "
                    f"{stats['win_rate_when_absent']:.0%} when absent"
                )
            elif stats["sample_present"] >= 10 and stats["lift"] < -0.10:
                recs.append(
                    f"Decrease weight on {factor} — "
                    f"actually hurts ({stats['lift']:+.0%} lift)"
                )

        for regime, strat in regimes.items():
            if strat.sample_size >= 30 and strat.win_rate < 0.45:
                recs.append(
                    f"Reduce trading in {regime} — only {strat.win_rate:.0%} "
                    f"win rate ({strat.sample_size} trades)"
                )
            elif strat.sample_size >= 30 and strat.win_rate >= 0.80:
                recs.append(
                    f"{regime} is your edge — {strat.win_rate:.0%} win rate, "
                    f"lean in"
                )

        for pair, prof in pairs.items():
            if prof.total_trades >= 20 and prof.recommendation == "AVOID":
                recs.append(
                    f"Avoid {pair} — {prof.win_rate:.0%} win rate "
                    f"({prof.total_trades} trades)"
                )

        for session, prof in sessions.items():
            if prof.total_trades >= 15 and prof.recommendation == "AGGRESSIVE":
                recs.append(
                    f"{session} is your goldmine — "
                    f"{prof.win_rate:.0%} win rate, increase aggression"
                )
            elif prof.total_trades >= 15 and prof.recommendation == "AVOID":
                recs.append(
                    f"Avoid {session} — "
                    f"{prof.win_rate:.0%} win rate, stay out"
                )

        self._last_recommendations = recs
        return recs


MLAdapter = AdaptiveOptimizer
