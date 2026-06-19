"""APEX TRADER — Dashboard Adaptive Optimizer Insights Mixin."""

from loguru import logger


class MLInsightsMixin:
    """get_ml_insights() — adaptive optimizer stats for the dashboard."""

    def get_ml_insights(self) -> dict:
        empty = {"score_adjustments": {}, "regime_stats": {}, "session_stats": {}, "pair_stats": {}}
        ctx = getattr(self, "_system_context", None)
        ml = None
        if ctx is not None and getattr(ctx, "ml_adapter", None) is not None:
            ml = ctx.ml_adapter
        elif self.is_live:
            ml = getattr(self._trading_loop, "ml", None)
        if ml is None:
            return empty
        try:
            optimizer = getattr(ml, "score_optimizer", None) or getattr(ml, "optimizer", None)
            regime_learner = getattr(ml, "regime_learner", None)
            session_learner = getattr(ml, "session_learner", None)
            pair_learner = getattr(ml, "pair_learner", None)

            score_adjustments: dict = {}
            if optimizer is not None:
                defaults = getattr(optimizer, "_DEFAULT_WEIGHTS", {})
                current = getattr(optimizer, "_weights", defaults)
                for k, v in current.items():
                    score_adjustments[k] = round(v - defaults.get(k, v), 1)

            regime_stats: dict = {}
            if regime_learner is not None and hasattr(regime_learner, "_strategies"):
                for regime, strategy in regime_learner._strategies.items():
                    regime_stats[regime] = {
                        "win_rate": round(getattr(strategy, "win_rate", 0) * 100, 1),
                        "trades": getattr(strategy, "trade_count", 0),
                        "recommendation": "trade" if getattr(strategy, "should_trade", True) else "skip",
                    }

            session_stats: dict = {}
            if session_learner is not None and hasattr(session_learner, "_profiles"):
                for session, profile in session_learner._profiles.items():
                    session_stats[session] = {
                        "win_rate": round(getattr(profile, "win_rate", 0) * 100, 1),
                        "trades": getattr(profile, "trade_count", 0),
                        "aggression": getattr(profile, "aggression", "normal"),
                    }

            pair_stats: dict = {}
            if pair_learner is not None and hasattr(pair_learner, "_profiles"):
                for pair, profile in pair_learner._profiles.items():
                    pair_stats[pair] = {
                        "win_rate": round(getattr(profile, "win_rate", 0) * 100, 1),
                        "trades": getattr(profile, "trade_count", 0),
                        "size_mult": round(getattr(profile, "size_multiplier", 1.0), 2),
                    }

            return {
                "score_adjustments": score_adjustments,
                "regime_stats": regime_stats,
                "session_stats": session_stats,
                "pair_stats": pair_stats,
            }
        except Exception as exc:
            logger.warning("Adaptive optimizer insights error: {}", exc)
            return empty
