"""
APEX TRADER — Dashboard Learning-Layer Mixin

The adaptive learning layer produces a lot of output that was never visible on
the dashboard.  This mixin surfaces it, read-only, in one place:

  * **Signal Ledger** — every directional read recorded + graded on price move.
  * **Emitter Feedback** — per-module accuracy, split traded vs blocked.
  * **Vote Calibrator** — calibrated per-module vote-weight multipliers.
  * **Per-class Score Optimizer** — confluence weight profiles per asset class.
  * **Pair Learner** — continuous per-pair size multipliers + entry/mgmt split.
  * **Tuner Agent** — central tuning authority status + recent tune audit.
  * **Counterfactual** — leave-one-out per-module marginal attribution (L4).

Everything here only *reads* from the live components (or returns a graceful
empty/disabled shape when a component is off, idle, or has no data yet).  It
never makes or mutates a decision, and never touches a learning component's
internals beyond the read-only accessors the existing ML panel already uses.
"""

from __future__ import annotations

from typing import Any

from loguru import logger


def _round(x: Any, ndigits: int = 4) -> float:
    try:
        return round(float(x), ndigits)
    except (TypeError, ValueError):
        return 0.0


def _idle(extra: dict | None = None) -> dict:
    base = {"enabled": False, "source": "idle"}
    if extra:
        base.update(extra)
    return base


class LearningMixin:
    """get_learning() — the adaptive learning layer, made visible."""

    # ── Component handles (all optional, all read-only) ──────────────────────
    def _loop(self) -> Any:
        return self._trading_loop if self.is_live else None

    def _config(self) -> Any:
        return getattr(self._loop(), "config", None)

    def _signal_ledger_obj(self) -> Any:
        return getattr(self._loop(), "_signal_ledger", None)

    def _emitter_feedback_obj(self) -> Any:
        return getattr(self._loop(), "_emitter_feedback", None)

    def _vote_calibrator_obj(self) -> Any:
        return getattr(self._loop(), "_vote_calibrator", None)

    def _ml_obj(self) -> Any:
        return getattr(self._loop(), "ml", None)

    def _score_optimizer_obj(self) -> Any:
        ml = self._ml_obj()
        return getattr(ml, "optimizer", None) if ml is not None else None

    def _pair_learner_obj(self) -> Any:
        ml = self._ml_obj()
        return getattr(ml, "pair_learner", None) if ml is not None else None

    def _tuner_agent_obj(self) -> Any:
        return getattr(self._loop(), "_tuner_agent", None)

    def _counterfactual_obj(self) -> Any:
        return getattr(self._loop(), "_counterfactual", None)

    def _interaction_obj(self) -> Any:
        return getattr(self._loop(), "_interaction_analyzer", None)
    def _param_evolution_obj(self) -> Any:
        return getattr(self._loop(), "_param_evolver", None)

    def _signal_discovery_obj(self) -> Any:
        return getattr(self._loop(), "_signal_discovery", None)

    def _virtual_manager_obj(self) -> Any:
        return getattr(self._loop(), "_virtual_signal_manager", None)

    def _virtual_registry_obj(self) -> Any:
        return getattr(self._loop(), "_virtual_registry", None)

    def _capital_allocator_obj(self) -> Any:
        return getattr(self._loop(), "_capital_allocator", None)

    # ── Aggregate ────────────────────────────────────────────────────────────
    def get_learning(self) -> dict:
        """Every learning-layer producer's output for the Learning panel."""
        return {
            "source": "live" if self.is_live else "idle",
            "signal_ledger": self._safe(self._learning_signal_ledger),
            "emitter_feedback": self._safe(self._learning_emitter_feedback),
            "vote_calibrator": self._safe(self._learning_vote_calibrator),
            "score_optimizer": self._safe(self._learning_score_optimizer),
            "pair_learner": self._safe(self._learning_pair_learner),
            "tuner_agent": self._safe(self._learning_tuner_agent),
            "counterfactual": self._safe(self._learning_counterfactual),
            "interactions": self._safe(self._learning_interactions),
            "param_evolution": self._safe(self._learning_param_evolution),
            "signal_discovery": self._safe(self._learning_signal_discovery),
            "virtual_modules": self._safe(self._learning_virtual_modules),
            "capital_allocation": self._safe(self._learning_capital_allocation),
        }

    @staticmethod
    def _safe(fn) -> dict:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[state_learning] {} failed: {}", getattr(fn, "__name__", fn), exc)
            return _idle({"error": str(exc)})

    # ── Signal Ledger ────────────────────────────────────────────────────────
    def _learning_signal_ledger(self) -> dict:
        ledger = self._signal_ledger_obj()
        cfg = getattr(self._config(), "signal_ledger", None)
        meta = {
            "check_intervals": list(getattr(cfg, "signal_grading_check_intervals", []) or []),
            "grading_delay_minutes": int(getattr(cfg, "signal_grading_delay_minutes", 0) or 0),
            "min_move_pct": _round(getattr(cfg, "signal_min_move_pct", 0.0), 3),
            "grading_enabled": bool(getattr(cfg, "signal_grading_enabled", False)),
        }
        if ledger is None:
            return _idle(meta)

        # Per-emitter accuracy (graded only) + recent graded signals.
        emitters_map = ledger.get_emitter_accuracy_all() or {}
        emitters = sorted(emitters_map.values(), key=lambda e: e.get("total", 0), reverse=True)
        graded = ledger.get_graded_signals(lookback=2000)
        all_rows = ledger.get_graded_signals(lookback=2000, include_ungraded=True)

        recent = []
        for r in graded[:40]:
            recent.append({
                "pair": str(r.get("pair", "")),
                "emitter": str(r.get("emitter", "")),
                "direction": str(r.get("direction", "")),
                "strength": _round(r.get("strength", 0.0), 3),
                "trade_opened": bool(r.get("trade_opened")),
                "gate_blocked_by": str(r.get("gate_blocked_by") or ""),
                "direction_correct": r.get("direction_correct"),
                "max_favorable_pct": _round(r.get("max_favorable_move_pct", 0.0), 3),
                "max_adverse_pct": _round(r.get("max_adverse_move_pct", 0.0), 3),
            })

        total_graded = len(graded)
        correct = sum(1 for r in graded if r.get("direction_correct"))
        return {
            "enabled": True,
            "source": "live",
            "total_recorded": len(all_rows),
            "total_graded": total_graded,
            "overall_accuracy": _round(correct / total_graded, 4) if total_graded else 0.0,
            "emitters": emitters,
            "recent": recent,
            **meta,
        }

    # ── Emitter Feedback ─────────────────────────────────────────────────────
    def _learning_emitter_feedback(self) -> dict:
        service = self._emitter_feedback_obj()
        cfg = getattr(self._config(), "signal_ledger", None)
        if service is None:
            return _idle({"feedback_flag": bool(getattr(cfg, "emitter_feedback_enabled", False))})

        summaries = service.get_all_emitter_summaries(lookback=200) or {}
        emitters = []
        for resp in summaries.values():
            emitters.append({
                "emitter": getattr(resp, "emitter", ""),
                "total_signals": int(getattr(resp, "total_signals", 0) or 0),
                "traded_signals": int(getattr(resp, "traded_signals", 0) or 0),
                "blocked_signals": int(getattr(resp, "blocked_signals", 0) or 0),
                "accuracy_all": _round(getattr(resp, "accuracy_all", 0.0), 4),
                "accuracy_traded": _round(getattr(resp, "accuracy_traded", 0.0), 4),
                "accuracy_blocked": _round(getattr(resp, "accuracy_blocked", 0.0), 4),
                "signal_value_when_blocked": _round(
                    getattr(resp, "signal_value_when_blocked", 0.0), 4
                ),
            })
        emitters.sort(key=lambda e: e["total_signals"], reverse=True)

        gate_map = service.get_gate_effectiveness(lookback=500) or {}
        gates = sorted(gate_map.values(), key=lambda g: g.get("blocked", 0), reverse=True)
        return {
            "enabled": True,
            "source": "live",
            "emitters": emitters,
            "gates": gates,
        }

    # ── Vote Calibrator ──────────────────────────────────────────────────────
    def _learning_vote_calibrator(self) -> dict:
        vc = self._vote_calibrator_obj()
        if vc is None:
            return _idle()

        state = vc.get_state() or {}
        multipliers = state.get("multipliers", {}) or {}
        last = vc.last_calibration
        last_dict = last.to_dict() if last is not None else {}
        sample_sizes = last_dict.get("sample_sizes", {}) or {}
        accuracies = last_dict.get("accuracies", {}) or {}
        raw_acc = last_dict.get("raw_accuracies", {}) or {}
        min_signals = int(state.get("min_signals", 0) or 0)

        # Total modules tracked: union of published multipliers + last-pass inputs.
        names = set(multipliers) | set(sample_sizes)
        try:
            from adaptive.vote_calibrator import DEFAULT_VOTE_MODULES

            names |= set(DEFAULT_VOTE_MODULES)
        except Exception:  # noqa: BLE001
            pass

        modules = []
        calibrated_n = 0
        for name in sorted(names):
            n = int(sample_sizes.get(name, 0) or 0)
            qualified = n >= min_signals if min_signals else False
            if qualified:
                calibrated_n += 1
            modules.append({
                "module": name,
                "multiplier": _round(multipliers.get(name, 1.0), 4),
                "sample_size": n,
                "accuracy": _round(accuracies.get(name, 0.0), 4),
                "raw_accuracy": _round(raw_acc.get(name, 0.0), 4),
                "calibrated": qualified,
            })
        modules.sort(key=lambda m: m["multiplier"], reverse=True)

        return {
            "enabled": bool(getattr(vc, "enabled", False)),
            "source": "live",
            "method": str(state.get("method", "")),
            "temperature": _round(state.get("temperature", 1.0), 3),
            "floor": _round(state.get("floor", 0.0), 3),
            "ceiling": _round(state.get("ceiling", 0.0), 3),
            "min_signals": min_signals,
            "qualifying_modules": int(last_dict.get("qualifying_modules", calibrated_n) or 0),
            "module_count": len(modules),
            "calibrated_count": calibrated_n,
            "last_reason": str(last_dict.get("reason", "")),
            "skipped": bool(last_dict.get("skipped", False)),
            "modules": modules,
        }

    # ── Per-class Score Optimizer ────────────────────────────────────────────
    def _learning_score_optimizer(self) -> dict:
        opt = self._score_optimizer_obj()
        if opt is None:
            return _idle()

        global_w = opt.current_weights.as_dict() if opt.current_weights is not None else {}
        class_weights = getattr(opt, "class_weights", {}) or {}
        classes = []
        for cls, weights in class_weights.items():
            wd = weights.as_dict() if weights is not None else {}
            diverged = wd != global_w
            classes.append({
                "asset_class": cls,
                "weights": wd,
                "diverged": diverged,
            })
        classes.sort(key=lambda c: c["asset_class"])

        return {
            "enabled": bool(getattr(opt, "per_class", False)),
            "source": "live",
            "min_trades_per_class": int(getattr(opt, "min_trades_per_class", 0) or 0),
            "class_shrinkage_strength": _round(getattr(opt, "class_shrinkage_strength", 0.0), 3),
            "global_weights": global_w,
            "classes": classes,
            "class_count": len(classes),
        }

    # ── Pair Learner ─────────────────────────────────────────────────────────
    def _learning_pair_learner(self) -> dict:
        pl = self._pair_learner_obj()
        if pl is None:
            return _idle()

        profiles = getattr(pl, "_profiles", {}) or {}
        rows = []
        for pair, prof in profiles.items():
            try:
                mult = pl.get_pair_multiplier(pair)
            except Exception:  # noqa: BLE001
                mult = 0.0
            rows.append({
                "pair": str(pair),
                "win_rate": _round(getattr(prof, "win_rate", 0.0), 4),
                "trades": int(getattr(prof, "total_trades", 0) or 0),
                "recommendation": str(getattr(prof, "recommendation", "")),
                "multiplier": _round(mult, 4),
                "entry_accuracy": (
                    _round(prof.entry_accuracy, 4)
                    if getattr(prof, "entry_accuracy", None) is not None else None
                ),
                "management_score": (
                    _round(prof.management_score, 4)
                    if getattr(prof, "management_score", None) is not None else None
                ),
                "optimal_sl_r": (
                    _round(prof.optimal_sl_r, 3)
                    if getattr(prof, "optimal_sl_r", None) is not None else None
                ),
            })
        rows.sort(key=lambda r: r["multiplier"], reverse=True)

        avoid = [r for r in rows if r["recommendation"] == "AVOID"]
        return {
            "enabled": True,
            "source": "live",
            "continuous_enabled": bool(getattr(pl, "continuous_enabled", False)),
            "pair_count": len(rows),
            "recommended": list(pl.get_recommended_pairs() or []),
            "avoid_count": len(avoid),
            "pairs": rows,
        }

    # ── Tuner Agent ──────────────────────────────────────────────────────────
    def _learning_tuner_agent(self) -> dict:
        agent = self._tuner_agent_obj()
        if agent is None:
            return _idle()

        status = agent.get_system_tuning_status() or {}
        registered = status.get("registered_tunables", {}) or {}
        tunables = []
        disabled = []
        for name, info in registered.items():
            row = {
                "name": name,
                "frequency": str(info.get("frequency", "")),
                "tune_count": int(info.get("tune_count", 0) or 0),
                "last_tune_time": info.get("last_tune_time"),
                "last_success": info.get("last_success"),
                "consecutive_failures": int(info.get("consecutive_failures", 0) or 0),
                "disabled": bool(info.get("disabled", False)),
            }
            tunables.append(row)
            if row["disabled"]:
                disabled.append(name)
        tunables.sort(key=lambda t: t["name"])

        audit = []
        for r in (agent.get_audit_log(limit=20) or []):
            audit.append({
                "timestamp": r.get("timestamp"),
                "tunable_name": str(r.get("tunable_name", "")),
                "success": bool(r.get("success")),
                "changed": bool(r.get("changed")),
                "skipped": bool(r.get("skipped")),
                "rollback_performed": bool(r.get("rollback_performed")),
                "reason": str(r.get("reason") or ""),
                "error": str(r.get("error") or ""),
            })

        return {
            "enabled": bool(status.get("agent_enabled", False)),
            "source": "live",
            "is_sole_authority": bool(status.get("is_sole_authority", False)),
            "expected_count": int(status.get("expected_count", 0) or 0),
            "registered_count": int(status.get("registered_count", 0) or 0),
            "unregistered_expected": list(status.get("unregistered_expected", []) or []),
            "disabled_tunables": disabled,
            "bypass_attempts": list(status.get("bypass_attempts", []) or []),
            "tunables": tunables,
            "audit": audit,
        }

    # ── Counterfactual Attribution (L4) ──────────────────────────────────────
    def _learning_counterfactual(self) -> dict:
        engine = self._counterfactual_obj()
        cfg = getattr(self._config(), "counterfactual", None)
        meta = {
            "lookback": int(getattr(cfg, "attribution_lookback", 0) or 0),
            "interval": int(getattr(cfg, "attribution_interval", 0) or 0),
            "min_trades": int(getattr(cfg, "min_trades_for_attribution", 0) or 0),
        }
        if engine is None:
            return _idle(meta)

        cached = engine.get_cached_attributions() or {}
        modules = []
        for m in cached.get("modules", []) or []:
            modules.append({
                "module": str(m.get("module", "")),
                "trades_involved": int(m.get("trades_involved", 0) or 0),
                "decisive_trades": int(m.get("decisive_trades", 0) or 0),
                "decisive_r": _round(m.get("decisive_r", 0.0), 3),
                "supporting_trades": int(m.get("supporting_trades", 0) or 0),
                "opposing_trades": int(m.get("opposing_trades", 0) or 0),
                "marginal_r": _round(m.get("marginal_r", 0.0), 3),
                "expectancy_when_decisive": _round(m.get("expectancy_when_decisive", 0.0), 3),
                "sharpe_contribution": _round(m.get("sharpe_contribution", 0.0), 3),
                "drawdown_contribution": _round(m.get("drawdown_contribution", 0.0), 3),
                "better_off_without": bool(m.get("better_off_without", False)),
                "r_difference": _round(m.get("r_difference", 0.0), 3),
            })
        # Already ranked best→worst by the engine; keep that order.
        return {
            "enabled": bool(getattr(engine, "enabled", False)),
            "source": "live",
            "computed_at": cached.get("computed_at"),
            "trades_analyzed": int(cached.get("trades_analyzed", 0) or 0),
            "module_count": int(cached.get("module_count", 0) or 0),
            "modules": modules,
            **meta,
        }

    # ── Module Interaction Discovery (L5b) ────────────────────────────────────
    def _learning_interactions(self) -> dict:
        analyzer = self._interaction_obj()
        cfg = getattr(self._config(), "interaction", None)
        meta = {
            "lookback": int(getattr(cfg, "interaction_lookback", 0) or 0),
            "interval": int(getattr(cfg, "interaction_interval", 0) or 0),
            "toxic_threshold": _round(getattr(cfg, "toxic_threshold", 0.0), 4),
            "synergy_threshold": _round(getattr(cfg, "synergy_threshold", 0.0), 4),
        }
        if analyzer is None:
            return _idle(meta)

        cached = analyzer.get_cached() or {}

        def _pair(p: dict) -> dict:
            return {
                "module_a": str(p.get("module_a", "")),
                "module_b": str(p.get("module_b", "")),
                "removal_delta_a": _round(p.get("removal_delta_a", 0.0), 3),
                "removal_delta_b": _round(p.get("removal_delta_b", 0.0), 3),
                "removal_delta_ab": _round(p.get("removal_delta_ab", 0.0), 3),
                "interaction_effect": _round(p.get("interaction_effect", 0.0), 3),
                "relationship": str(p.get("relationship", "")),
            }

        pairs = [_pair(p) for p in (cached.get("pairs", []) or [])]
        toxic = [_pair(p) for p in (cached.get("toxic_pairs", []) or [])]
        synergy = [_pair(p) for p in (cached.get("synergy_pairs", []) or [])]

        opt = cached.get("optimal_subset", {}) or {}
        optimal = {
            "active_modules": list(opt.get("active_modules", []) or []),
            "shadow_modules": list(opt.get("shadow_modules", []) or []),
            "total_r": _round(opt.get("total_r", 0.0), 3),
            "baseline_r": _round(opt.get("baseline_r", 0.0), 3),
            "improvement": _round(opt.get("improvement", 0.0), 3),
            "sharpe": _round(opt.get("sharpe", 0.0), 3),
            "max_drawdown": _round(opt.get("max_drawdown", 0.0), 3),
            "trades_taken": int(opt.get("trades_taken", 0) or 0),
            "search": str(opt.get("search", "")),
        }
        return {
            "enabled": bool(getattr(analyzer, "enabled", False)),
            "source": "live",
            "computed_at": cached.get("computed_at"),
            "trades_analyzed": int(cached.get("trades_analyzed", 0) or 0),
            "module_count": int(cached.get("module_count", 0) or 0),
            "modules": list(cached.get("modules", []) or []),
            "search": str(cached.get("search", "")),
            "pairs": pairs,
            "toxic_pairs": toxic,
            "synergy_pairs": synergy,
            "optimal_subset": optimal,
        }

    # ── Parameter Evolution (L5a) ─────────────────────────────────────────────
    def _learning_param_evolution(self) -> dict:
        evolver = self._param_evolution_obj()
        cfg = getattr(self._config(), "param_evolution", None)
        meta = {
            "replay_lookback": int(getattr(cfg, "replay_lookback", 0) or 0),
            "shadow_validation_trades": int(getattr(cfg, "shadow_validation_trades", 0) or 0),
            "significance_threshold": _round(getattr(cfg, "significance_threshold", 0.0), 4),
        }
        if evolver is None:
            return _idle(meta)
        state = evolver.get_state() or {}
        shadows = []
        for s in state.get("active_shadows", []) or []:
            shadows.append({
                "param_name": str(s.get("param_name", "")),
                "current_value": _round(s.get("current_value", 0.0), 4),
                "proposed_value": _round(s.get("proposed_value", 0.0), 4),
                "state": str(s.get("state", "")),
                "shadow_trades": int(s.get("shadow_trades", 0) or 0),
                "improvement_per_trade": _round(s.get("improvement_per_trade", 0.0), 4),
            })
        promotions = []
        for p in state.get("recent_promotions", []) or []:
            promotions.append({
                "param_name": str(p.get("param_name", "")),
                "old_value": _round(p.get("old_value", 0.0), 4),
                "new_value": _round(p.get("new_value", 0.0), 4),
                "decision": str(p.get("decision", "")),
                "decided_at": p.get("decided_at"),
            })
        return {
            "enabled": bool(state.get("enabled", False)),
            "source": "live",
            "evolvable_params": list(state.get("evolvable_params", []) or []),
            "active_shadow_count": int(state.get("active_shadow_count", 0) or 0),
            "active_shadows": shadows,
            "recent_promotions": promotions,
            **meta,
        }

    # ── Synthetic Signal Discovery (L5c) ──────────────────────────────────────
    def _learning_signal_discovery(self) -> dict:
        engine = self._signal_discovery_obj()
        cfg = getattr(self._config(), "signal_discovery", None)
        meta = {
            "lookback": int(getattr(cfg, "discovery_lookback", 0) or 0),
            "min_edge_r": _round(getattr(cfg, "min_edge_r", 0.0), 3),
        }
        if engine is None:
            return _idle(meta)
        state = engine.get_state() or {}
        rules = []
        for r in state.get("rules", []) or []:
            rules.append({
                "label": str(r.get("label", "")),
                "size": int(r.get("size", 0) or 0),
                "support": int(r.get("support", 0) or 0),
                "win_rate": _round(r.get("win_rate", 0.0), 4),
                "expectancy": _round(r.get("expectancy", 0.0), 4),
                "edge": _round(r.get("edge", 0.0), 4),
                "train_edge": _round(r.get("train_edge", 0.0), 4),
                "test_edge": _round(r.get("test_edge", 0.0), 4),
                "qualifies": bool(r.get("qualifies", False)),
                "active": bool(r.get("active", False)),
                "p_value": _round(r.get("p_value", 1.0), 6),
                "wf_ratio": _round(r.get("wf_ratio", 0.0), 4),
                "score": _round(r.get("score", 0.0), 4),
                "confirmations": int(r.get("confirmations", 0) or 0),
            })
        return {
            "enabled": bool(state.get("enabled", False)),
            "source": "live",
            "computed_at": state.get("computed_at"),
            "trades_analyzed": int(state.get("trades_analyzed", 0) or 0),
            "baseline_expectancy": _round(state.get("baseline_expectancy", 0.0), 4),
            "rule_count": int(state.get("rule_count", 0) or 0),
            "qualifying_count": int(state.get("qualifying_count", 0) or 0),
            "active_count": int(state.get("active_count", 0) or 0),
            "max_active_signals": int(state.get("max_active_signals", 0) or 0),
            "bonferroni_alpha": _round(state.get("bonferroni_alpha", 0.0), 6),
            "walk_forward_ratio_threshold": _round(
                state.get("walk_forward_ratio_threshold", 0.0), 4
            ),
            "rules": rules[:40],
            **meta,
        }

    # ── Virtual Voting Modules (L5c shadow → promote → retire) ────────────────
    def _learning_virtual_modules(self) -> dict:
        manager = self._virtual_manager_obj()
        registry = self._virtual_registry_obj()
        cfg = getattr(self._config(), "signal_discovery", None)
        meta = {
            "promotion_flag": bool(getattr(cfg, "virtual_promotion_enabled", False)),
            "kill_switch": bool(getattr(cfg, "signal_discovery_enabled", False)),
            "shadow_trades_required": int(getattr(cfg, "shadow_trades_required", 0) or 0),
            "min_shadow_accuracy": _round(getattr(cfg, "min_shadow_accuracy", 0.0), 3),
            "max_active": int(getattr(cfg, "max_active_signals", 0) or 0),
        }
        # Prefer the manager (adds lifecycle summary); fall back to the registry.
        status = None
        last_eval = None
        promotion_enabled = meta["promotion_flag"]
        if manager is not None:
            try:
                status = manager.get_status() or {}
                last_eval = status.get("last_evaluation")
                promotion_enabled = bool(status.get("promotion_enabled", promotion_enabled))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[state_learning] virtual manager status failed: {}", exc)
                status = None
        if status is None and registry is not None:
            status = registry.get_status() or {}
        if status is None:
            return _idle(meta)

        modules = []
        for m in status.get("modules", []) or []:
            definition = m.get("definition", {}) or {}
            modules.append({
                "name": str(m.get("name", "")),
                "mode": str(m.get("mode", "")),
                "weight": _round(m.get("weight", 0.0), 4),
                "effective_weight": _round(m.get("effective_weight", 0.0), 4),
                "restart_shadow": bool(m.get("restart_shadow", False)),
                "vote_direction": str(definition.get("vote_direction", "")),
                "base_confidence": _round(definition.get("base_confidence", 0.0), 3),
                "win_rate": _round(definition.get("win_rate", 0.0), 4),
                "edge": _round(definition.get("edge", 0.0), 4),
                "source_label": str(definition.get("source_label", "")),
                "seconds_in_mode": _round(m.get("seconds_in_mode", 0.0), 1),
                "reason": str(m.get("reason", "")),
            })
        # Active first, then by weight.
        modules.sort(key=lambda r: (r["mode"] != "ACTIVE", -r["weight"]))

        transitions = []
        if registry is not None:
            try:
                for t in (registry.get_transitions(limit=30) or []):
                    transitions.append({
                        "timestamp": t.get("timestamp"),
                        "name": str(t.get("name", "")),
                        "old_mode": str(t.get("old_mode", "")),
                        "new_mode": str(t.get("new_mode", "")),
                        "weight": _round(t.get("weight", 0.0), 4),
                        "accuracy": _round(t.get("accuracy", 0.0), 4),
                        "marginal_r": _round(t.get("marginal_r", 0.0), 4),
                        "reason": str(t.get("reason", "")),
                    })
            except Exception as exc:  # noqa: BLE001
                logger.debug("[state_learning] virtual transitions failed: {}", exc)

        return {
            "enabled": bool(status.get("enabled", False)),
            "source": "live",
            "promotion_enabled": promotion_enabled,
            "counts": status.get("counts", {}) or {},
            "module_count": int(status.get("module_count", 0) or 0),
            "restart_pending": int(status.get("restart_pending", 0) or 0),
            "last_evaluation": last_eval,
            "modules": modules,
            "transitions": transitions,
            **meta,
        }

    # ── Capital Allocation Engine (L5.5a) ─────────────────────────────────────
    def _learning_capital_allocation(self) -> dict:
        allocator = self._capital_allocator_obj()
        cfg = getattr(self._config(), "capital_allocation", None)
        meta = {
            "rebalance_interval_trades": int(getattr(cfg, "rebalance_interval_trades", 0) or 0),
            "min_trades_for_scoring": int(getattr(cfg, "min_trades_for_scoring", 0) or 0),
            "config_flag": bool(getattr(cfg, "enabled", False)),
        }
        if allocator is None:
            return _idle(meta)
        state = allocator.get_state() or {}
        fingerprints = []
        for f in state.get("fingerprints", []) or []:
            fingerprints.append({
                "fingerprint": str(f.get("fingerprint", "")),
                "trades": int(f.get("trades", 0) or 0),
                "exp_short": _round(f.get("exp_short", 0.0), 4),
                "exp_medium": _round(f.get("exp_medium", 0.0), 4),
                "exp_long": _round(f.get("exp_long", 0.0), 4),
                "blended_score": _round(f.get("blended_score", 0.0), 4),
                "allocation": _round(f.get("allocation", 0.0), 4),
                "sizing_multiplier": _round(f.get("sizing_multiplier", 1.0), 4),
            })
        rebalances = []
        for rb in state.get("rebalance_history", []) or []:
            rebalances.append({
                "ts": rb.get("ts"),
                "fingerprints": int(rb.get("fingerprints", 0) or 0),
                "max_shift": _round(rb.get("max_shift", 0.0), 4),
                "floored": int(rb.get("floored", 0) or 0),
            })
        return {
            "enabled": bool(state.get("enabled", False)),
            "source": "live",
            "active": bool(state.get("active", False)),
            "total_trades": int(state.get("total_trades", 0) or 0),
            "trades_since_rebalance": int(state.get("trades_since_rebalance", 0) or 0),
            "last_rebalance_ts": state.get("last_rebalance_ts"),
            "horizon_weights": state.get("horizon_weights", {}) or {},
            "min_allocation": _round(state.get("min_allocation", 0.0), 4),
            "max_allocation_shift": _round(state.get("max_allocation_shift", 0.0), 4),
            "fingerprint_count": int(state.get("fingerprint_count", 0) or 0),
            "fingerprints": fingerprints,
            "rebalance_history": rebalances,
            **meta,
        }
