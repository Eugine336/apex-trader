"""Risk, portfolio heat, and margin methods for TradingLoop."""
from __future__ import annotations

import time as _time
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from brain import OpenTrade
from config import INSTRUMENT_REGISTRY
from platform_context import build_context_for_symbol
from risk.portfolio_risk_state import (
    PortfolioRiskState,
    PortfolioRiskSnapshot,
    PositionRisk,
    EmergencyTriggerSnapshot,
    evaluate_emergency_triggers,
    compute_position_risk_dollars,
    compute_live_heat_pct,
    is_eligible_for_defensive_breakeven,
    rank_positions_weakest_first,
)


class RiskHeatMarginMixin:
    """Mixin providing portfolio heat, margin, and emergency risk methods."""

    def _check_portfolio_heat(self) -> None:
        """
        Compute live capital-at-risk heat and feed the portfolio risk
        state machine.  When in DEFENSIVE, advance eligible positions
        to breakeven.  When in REDUCING and reduction enabled, trim the
        weakest position (Phase 4b).
        """
        cfg = self.config.risk
        if not cfg.portfolio_heat_enabled:
            return
        if not self.managed_positions:
            self._current_portfolio_heat = 0.0
            self._account_risk.clear_heat()
            return

        # Per-account heat: group positions by their account silo and divide
        # each account's open risk by THAT account's balance — never a mixed
        # denominator. Refresh each distinct account's balance once per cycle.
        acct_balance: dict[str, float] = {}
        acct_risk_sum: dict[str, float] = {}
        acct_unrealized: dict[str, float] = {}
        position_risks: list[PositionRisk] = []

        for oid, pos in list(self.managed_positions.items()):
            acct = self._account_key(pos.symbol)
            if acct not in acct_balance:
                try:
                    bal = self.platforms.get_platform_balance(pos.symbol)
                except Exception:
                    bal = 0.0
                if bal and bal > 0:
                    self._account_risk.update_balance(acct, bal)
                acct_balance[acct] = self._account_risk.balance(acct)

            tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
            at_be = bool(tm_trade and tm_trade.breakeven_active)

            info = INSTRUMENT_REGISTRY.get(pos.symbol)
            pip_size = info.pip_size if info else 0.0001
            pip_val = info.pip_value_per_lot if info else 10.0

            ctx = build_context_for_symbol(pos.symbol)
            is_deriv = ctx.uses_stake

            risk_dollars, is_fallback = compute_position_risk_dollars(
                direction=pos.direction,
                entry_price=pos.entry_price,
                sl=pos.sl,
                lots=pos.lots,
                pip_size=pip_size,
                pip_value_per_lot=pip_val,
                at_breakeven=at_be,
                stake_usd=pos.stake_usd,
                multiplier=pos.multiplier,
                is_deriv_stake=is_deriv,
            )
            if is_fallback:
                risk_dollars = (acct_balance.get(acct, 0.0) or 0.0) * cfg.risk_per_trade_pct / 100.0
                logger.debug(
                    "[PortfolioRisk] {} fallback to static proxy ${:.2f}",
                    pos.symbol, risk_dollars,
                )
            acct_risk_sum[acct] = acct_risk_sum.get(acct, 0.0) + risk_dollars
            acct_unrealized[acct] = acct_unrealized.get(acct, 0.0) + (
                getattr(pos, "broker_pnl", 0.0) or 0.0
            )
            position_risks.append(PositionRisk(
                order_id=oid,
                symbol=pos.symbol,
                direction=pos.direction,
                risk_dollars=risk_dollars,
                is_at_breakeven=at_be,
                is_fallback=is_fallback,
            ))

        # Store each account's heat for the per-account entry gate.
        self._account_risk.clear_heat()
        for acct, rsum in acct_risk_sum.items():
            bal = acct_balance.get(acct, 0.0)
            acct_heat = (rsum / bal * 100.0) if bal > 0 else 0.0
            self._account_risk.set_heat(acct, acct_heat)
            if acct_heat >= cfg.max_portfolio_heat_pct:
                logger.warning(
                    "🌡️ ACCOUNT HEAT {:.2f}% — '{}' | max {:.1f}% | equity ${:.2f}",
                    acct_heat, acct, cfg.max_portfolio_heat_pct, bal,
                )
                self._add_warning(
                    "warning",
                    f"Account '{acct}' heat {acct_heat:.2f}% — approaching limit",
                )

        # ── Per-account unrealized drawdown halt + flatten (Tier 2 #8/#9) ──
        # Feed each account's live OPEN P&L into the silo so a deep *unrealized*
        # loss trips the entry halt (not just realized), and flatten the account
        # if it breaches the harder flatten cap.
        for acct in list(acct_balance.keys()):
            self._account_risk.update_unrealized(acct, acct_unrealized.get(acct, 0.0))
            if (
                getattr(cfg, "daily_loss_flatten_enabled", True)
                and self._account_risk.flatten_breached(acct)
            ):
                logger.critical(
                    "🚨 ACCOUNT FLATTEN — '{}' combined daily loss {:.2f}% breached "
                    "−{:.1f}% flatten cap — flattening account positions",
                    acct, self._account_risk.combined_pnl_pct(acct),
                    self._account_risk.daily_loss_flatten_pct,
                )
                self._flatten_account(acct, "DAILY_LOSS_FLATTEN")

        # Portfolio-total heat (sum risk / sum balance) drives the global state
        # machine — a legitimate portfolio measure, no longer inflated by one
        # small account's denominator.
        equity = sum(acct_balance.values())
        live_heat = compute_live_heat_pct(position_risks, equity)
        self._current_portfolio_heat = live_heat

        if self._portfolio_risk_sm is None:
            return

        _current_risk = self.risk_engine.drawdown_guard.risk_map.get(
            self.risk_engine.drawdown_guard.mode, 0.005,
        )
        open_trades = [
            OpenTrade(pair=p.symbol, direction=p.direction, risk_pct=_current_risk)
            for p in self.managed_positions.values()
        ]
        exposure = self.correlation.calculate_exposure(open_trades)

        snapshot = PortfolioRiskSnapshot(
            live_heat_pct=live_heat,
            position_risks=position_risks,
            correlation_safe=exposure.is_safe,
            max_currency_exposure=exposure.max_single_currency_exposure,
            timestamp=_time.monotonic(),
        )
        transition = self._portfolio_risk_sm.evaluate(snapshot)

        # ── Emergency trigger evaluation (Phase 4c) ──────────────────────
        cfg_e = self.config.risk
        if cfg_e.portfolio_emergency_enabled and self._portfolio_risk_sm is not None:
            reconcile_age = (
                (datetime.now(timezone.utc) - self._last_reconcile_time).total_seconds()
                if self._last_reconcile_time is not None
                else 999999.0
            )
            broker_count: Optional[int] = None
            emergency_snap_fetch = self.platforms.get_open_positions_snapshot()
            if emergency_snap_fetch.confirmed_platforms:
                broker_count = len(emergency_snap_fetch.positions)
            else:
                logger.warning(
                    "[PortfolioRisk] EMERGENCY — broker exposure check skipped, "
                    "no platform confirmed (failed: {})",
                    emergency_snap_fetch.failed_platforms or "none connected",
                )
            emergency_snap = EmergencyTriggerSnapshot(
                live_heat_pct=live_heat,
                drawdown_mode=self.drawdown.mode.value,
                reconcile_age_seconds=reconcile_age,
                managed_count=len(self.managed_positions),
                broker_count=broker_count,
            )
            trigger_result = evaluate_emergency_triggers(
                emergency_snap,
                heat_emergency_pct=cfg_e.heat_emergency_pct,
                emergency_reconcile_failure_seconds=cfg_e.emergency_reconcile_failure_seconds,
                emergency_broker_exposure_tolerance=cfg_e.emergency_broker_exposure_tolerance,
            )
            if trigger_result.any_fired:
                transition = self._portfolio_risk_sm.escalate_to_emergency(
                    trigger_result, live_heat, exposure.is_safe,
                )

        if transition.changed:
            severity = "info"
            if transition.state in (
                PortfolioRiskState.DEFENSIVE,
                PortfolioRiskState.REDUCING,
                PortfolioRiskState.EMERGENCY,
            ):
                severity = "critical"
            self._add_warning(
                severity,
                f"[PortfolioRisk] {transition.reason}",
            )

        if transition.state == PortfolioRiskState.DEFENSIVE:
            self._apply_defensive_actions()
        elif transition.state == PortfolioRiskState.REDUCING:
            self._apply_defensive_actions()
            self._apply_reduction_actions(position_risks, exposure)
        elif transition.state == PortfolioRiskState.EMERGENCY:
            self._apply_defensive_actions()
            self._apply_reduction_actions(position_risks, exposure)
            self._apply_emergency_actions(position_risks, exposure, transition)

    def _apply_defensive_actions(self) -> None:
        """
        Non-destructive defensive actions: advance eligible positions to
        breakeven.  Respects per-position cooldowns and idempotency.
        No positions are closed or reduced (Phase 4b/4c).
        """
        cfg = self.config.risk
        now = _time.monotonic()

        for oid, pos in list(self.managed_positions.items()):
            if pos.at_breakeven:
                continue

            last_action = self._defensive_action_timestamps.get(oid, 0.0)
            if (now - last_action) < cfg.defensive_action_cooldown_seconds:
                continue

            tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
            if tm_trade is None:
                continue
            if tm_trade.breakeven_active:
                continue

            try:
                tick = self.platforms.get_price(pos.symbol)
                current_price = tick.bid if pos.direction == "BUY" else tick.ask
            except Exception as exc:
                logger.debug("[defensive] tick fetch for breakeven check failed, skipping position: {}", exc)
                continue

            eligible = is_eligible_for_defensive_breakeven(
                direction=pos.direction,
                entry_price=pos.entry_price,
                sl=pos.sl,
                current_price=current_price,
                tp1_hit=pos.tp1_hit,
                partial_closed=getattr(tm_trade, 'partial_closed', False),
                at_breakeven=pos.at_breakeven,
                be_eligible_r_multiple=cfg.be_eligible_r_multiple,
            )
            if not eligible:
                continue

            from management.partial_close import PartialCloseCalculator
            pip_size = tm_trade.pip_size if hasattr(tm_trade, 'pip_size') else 0.0001
            direction_norm = "LONG" if pos.direction.upper() in ("BUY", "LONG") else "SHORT"
            be_level = PartialCloseCalculator.calculate_breakeven_level(
                pos.entry_price, direction_norm, 2.0, pip_size,
            )
            is_improvement = (
                (pos.direction == "BUY" and be_level > pos.sl)
                or (pos.direction == "SELL" and be_level < pos.sl)
            )
            if not is_improvement:
                continue

            success = self.platforms.modify_trade(oid, pos.platform, new_sl=be_level)
            if success:
                pos.sl = be_level
                tm_trade.stop_loss = be_level
                tm_trade.breakeven_active = True
                pos.at_breakeven = True
                self.position_store.update_position(oid, sl=be_level, at_breakeven=True)
                self._defensive_action_timestamps[oid] = now
                logger.warning(
                    "[PortfolioRisk] DEFENSIVE BE — {} {} | SL→{:.5f} | eligible via {}",
                    pos.direction, pos.symbol, be_level,
                    "tp1_hit" if pos.tp1_hit else f">={cfg.be_eligible_r_multiple}R excursion",
                )

    def _apply_reduction_actions(
        self,
        position_risks: list[PositionRisk],
        exposure,
    ) -> None:
        """
        Graduated exposure reduction: trim the single weakest position
        by a configurable fraction.  Bypasses entry circuit-breaker /
        cooldowns / opportunity throttles — risk exits are not entries.

        Safety rails:
          • Gated behind portfolio_reduction_enabled (default OFF).
          • Per-position cooldown prevents repeated trims of the same position.
          • Hourly cap prevents a reduction storm.
          • Never fully closes a position (floor at broker minimum 0.01).
          • Orphan/unknown positions are neutral-ranked, never auto-targeted.
        """
        cfg = self.config.risk
        if not cfg.portfolio_reduction_enabled:
            return

        now = _time.monotonic()

        cutoff = now - 3600.0
        self._reduction_actions_this_hour = [
            t for t in self._reduction_actions_this_hour if t > cutoff
        ]
        if len(self._reduction_actions_this_hour) >= cfg.max_reductions_per_hour:
            logger.debug(
                "[PortfolioRisk] REDUCING — hourly cap reached ({}/{})",
                len(self._reduction_actions_this_hour), cfg.max_reductions_per_hour,
            )
            return

        total_risk = sum(pr.risk_dollars for pr in position_risks)
        pos_dicts: list[dict] = []

        for oid, pos in list(self.managed_positions.items()):
            try:
                tick = self.platforms.get_price(pos.symbol)
                current_price = tick.bid if pos.direction == "BUY" else tick.ask
            except Exception as exc:
                logger.debug("[heat] tick fetch for portfolio heat failed, skipping position: {}", exc)
                continue

            pr_match = next((pr for pr in position_risks if pr.order_id == oid), None)
            risk_dollars = pr_match.risk_dollars if pr_match else 0.0

            pos_dicts.append({
                "order_id": oid,
                "symbol": pos.symbol,
                "direction": pos.direction,
                "entry_price": pos.entry_price,
                "sl": pos.sl,
                "current_price": current_price,
                "score": pos.score,
                "regime": pos.regime,
                "entry_type": pos.entry_type,
                "open_time_utc": pos.open_time,
                "risk_dollars": risk_dollars,
                "lots": pos.lots,
            })

        if not pos_dicts:
            return

        ranked = rank_positions_weakest_first(pos_dicts, total_risk)
        if not ranked:
            return

        for candidate in ranked:
            if candidate.is_insufficient_data:
                continue

            oid = candidate.order_id
            pos = self.managed_positions.get(oid)
            if pos is None:
                continue

            last_trim = self._reduction_action_timestamps.get(oid, 0.0)
            if (now - last_trim) < cfg.reduction_action_cooldown_seconds:
                continue

            if pos.lots <= 0.01:
                continue

            trim_lots = round(pos.lots * cfg.reduction_partial_ratio, 2)
            trim_lots = max(0.01, trim_lots)
            remaining_after = round(pos.lots - trim_lots, 2)
            if remaining_after < 0.01:
                trim_lots = round(pos.lots - 0.01, 2)
                if trim_lots < 0.01:
                    continue

            pos_ctx = build_context_for_symbol(pos.symbol)
            if not pos_ctx.supports_partial_close:
                logger.debug(
                    "[PortfolioRisk] REDUCING — {} does not support partial close, skipping",
                    pos.symbol,
                )
                continue

            logger.warning(
                "[PortfolioRisk] REDUCING TRIM — {} {} | {:.2f} → {:.2f} lots "
                "| weakness={:.1f} ({}) | R={:.2f} | score={} | risk_share={:.1f}%",
                pos.direction, pos.symbol,
                pos.lots, remaining_after,
                candidate.weakness_score, candidate.reason,
                candidate.r_multiple, candidate.entry_score,
                candidate.risk_share_pct,
            )

            result = self.platforms.close_trade(oid, pos.platform, trim_lots)
            if result.success:
                pos.lots = remaining_after
                self.position_store.update_position(oid, lots=remaining_after)
                self._reduction_action_timestamps[oid] = now
                self._reduction_actions_this_hour.append(now)

                tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                if tm_trade:
                    tm_trade.remaining_size_lots = remaining_after

                self._add_warning(
                    "critical",
                    f"[PortfolioRisk] REDUCING — trimmed {pos.direction} {pos.symbol} "
                    f"by {trim_lots} lots (weakness={candidate.weakness_score:.1f})",
                )
                logger.warning(
                    "[PortfolioRisk] REDUCING TRIM SUCCESS — {} {} | trimmed {:.2f} lots, "
                    "remaining {:.2f} | heat was {:.2f}%",
                    pos.direction, pos.symbol, trim_lots, remaining_after,
                    self._current_portfolio_heat,
                )
            else:
                logger.warning(
                    "[PortfolioRisk] REDUCING TRIM FAILED — {} {} | broker rejected",
                    pos.direction, pos.symbol,
                )
            return

    def _apply_emergency_actions(
        self,
        position_risks: list[PositionRisk],
        exposure,
        transition,
    ) -> None:
        """
        Progressive full-close of weakest positions until heat returns
        below emergency threshold.  Bypasses entry circuit-breaker /
        cooldowns / opportunity throttles — risk exits are not entries.

        Precedence: margin_guardian owns margin events; this method owns
        heat/correlation-survival, drawdown-frozen, and reconcile-failure.
        Before every close, re-check managed_positions membership to
        prevent double-closing a position margin_guardian already closed.

        Safety rails:
          • Gated behind portfolio_emergency_enabled (default OFF).
          • Per-position cooldown prevents repeated closes.
          • Per-hour cap prevents an unbounded close storm.
          • emergency_max_closes_per_cycle limits closes per loop.
          • Orphan/unknown positions are neutral-ranked.
        """
        cfg = self.config.risk
        if not cfg.portfolio_emergency_enabled:
            return

        now = _time.monotonic()

        cutoff = now - 3600.0
        self._emergency_closes_this_hour = [
            t for t in self._emergency_closes_this_hour if t > cutoff
        ]
        if len(self._emergency_closes_this_hour) >= cfg.emergency_max_closes_per_hour:
            logger.warning(
                "[PortfolioRisk] EMERGENCY — hourly close cap reached ({}/{})",
                len(self._emergency_closes_this_hour), cfg.emergency_max_closes_per_hour,
            )
            return

        total_risk = sum(pr.risk_dollars for pr in position_risks)
        pos_dicts: list[dict] = []

        for oid, pos in list(self.managed_positions.items()):
            try:
                tick = self.platforms.get_price(pos.symbol)
                current_price = tick.bid if pos.direction == "BUY" else tick.ask
            except Exception as exc:
                logger.debug("[correlation] tick fetch for correlated risk failed, skipping position: {}", exc)
                continue

            pr_match = next((pr for pr in position_risks if pr.order_id == oid), None)
            risk_dollars = pr_match.risk_dollars if pr_match else 0.0

            pos_dicts.append({
                "order_id": oid,
                "symbol": pos.symbol,
                "direction": pos.direction,
                "entry_price": pos.entry_price,
                "sl": pos.sl,
                "current_price": current_price,
                "score": pos.score,
                "regime": pos.regime,
                "entry_type": pos.entry_type,
                "open_time_utc": pos.open_time,
                "risk_dollars": risk_dollars,
                "lots": pos.lots,
            })

        if not pos_dicts:
            return

        ranked = rank_positions_weakest_first(pos_dicts, total_risk)
        if not ranked:
            return

        closes_this_cycle = 0
        for candidate in ranked:
            if closes_this_cycle >= cfg.emergency_max_closes_per_cycle:
                break
            if len(self._emergency_closes_this_hour) >= cfg.emergency_max_closes_per_hour:
                break

            if candidate.is_insufficient_data:
                continue

            oid = candidate.order_id
            pos = self.managed_positions.get(oid)
            if pos is None:
                continue

            last_action = self._emergency_action_timestamps.get(oid, 0.0)
            if (now - last_action) < cfg.emergency_action_cooldown_seconds:
                continue

            logger.critical(
                "[PortfolioRisk] EMERGENCY CLOSE — {} {} | weakness={:.1f} ({}) "
                "| R={:.2f} | score={} | risk_share={:.1f}% | trigger={}",
                pos.direction, pos.symbol,
                candidate.weakness_score, candidate.reason,
                candidate.r_multiple, candidate.entry_score,
                candidate.risk_share_pct,
                transition.emergency_trigger or "sustained",
            )

            result = self.platforms.close_trade(oid, pos.platform)
            if result.success:
                self._record_closed_trade(
                    pos, result.close_price,
                    f"EMERGENCY_CLOSE({transition.emergency_trigger or 'sustained'})",
                    close_result=result,
                )
                self.managed_positions.pop(oid, None)
                self.position_store.remove_position(oid)
                self._emergency_action_timestamps[oid] = now
                self._emergency_closes_this_hour.append(now)
                closes_this_cycle += 1

                self._add_warning(
                    "critical",
                    f"[PortfolioRisk] EMERGENCY — closed {pos.direction} {pos.symbol} "
                    f"(weakness={candidate.weakness_score:.1f}, "
                    f"trigger={transition.emergency_trigger or 'sustained'})",
                )
                logger.critical(
                    "[PortfolioRisk] EMERGENCY CLOSE SUCCESS — {} {} | "
                    "heat was {:.2f}% | closes this hour: {}",
                    pos.direction, pos.symbol,
                    self._current_portfolio_heat,
                    len(self._emergency_closes_this_hour),
                )
            else:
                logger.warning(
                    "[PortfolioRisk] EMERGENCY CLOSE FAILED — {} {} | broker rejected",
                    pos.direction, pos.symbol,
                )

    def _get_margin_level(self, symbol: str | None = None) -> float:
        """Return the margin_level for the platform serving *symbol*.
        Returns 0.0 when unavailable (Deriv, disconnected) — callers must
        treat 0.0 as *unknown*, never as a flatten trigger."""
        try:
            if symbol:
                connector = self.platforms.get_connector(symbol)
            else:
                for i, c in enumerate(self.platforms.mt5_connectors):
                    if self.platforms._mt5_connected_flags[i]:
                        connector = c
                        break
                else:
                    return 0.0
            # margin_level is an MT5 concept. Deriv synthetic accounts have no
            # margin level (they report 0.0) — skip the call entirely so we
            # don't hammer Deriv's rate-limited balance endpoint for a constant.
            from platforms.mt5.mt5_connector import MT5Connector
            if not isinstance(connector, MT5Connector):
                return 0.0
            info = connector.get_account_info()
            return info.margin_level
        except Exception as exc:
            logger.warning("[margin] margin level fetch failed, returning 0.0: {}", exc)
            return 0.0

    def _check_margin_for_entry(self, symbol: str) -> tuple[bool, str]:
        ml = self._get_margin_level(symbol)
        if ml <= 0.0:
            return True, "margin_level unknown — skipping check"
        threshold = self.config.risk.margin_block_entry_pct
        if ml < threshold:
            return False, f"Margin level {ml:.0f}% < entry floor {threshold:.0f}%"
        return True, "Margin OK"

    def _margin_guardian_check(self) -> None:
        ml = self._get_margin_level()
        if ml <= 0.0:
            return
        risk_cfg = self.config.risk
        if ml < risk_cfg.margin_flatten_pct:
            logger.critical(
                "🚨 MARGIN GUARDIAN — margin {:.0f}% < flatten floor {:.0f}% — flattening all positions",
                ml, risk_cfg.margin_flatten_pct,
            )
            self._emergency_flatten_all()
        elif ml < risk_cfg.margin_warn_pct:
            logger.warning(
                "⚠️ MARGIN WARNING — margin {:.0f}% < warn level {:.0f}%",
                ml, risk_cfg.margin_warn_pct,
            )

    def _emergency_flatten_all(self) -> None:
        closed = 0
        failed_oids: list[str] = []
        for oid, pos in list(self.managed_positions.items()):
            try:
                result = self.platforms.close_trade(oid, pos.platform)
                if result.success:
                    self._record_closed_trade(
                        pos, result.close_price, "MARGIN_FLATTEN", close_result=result
                    )
                    # Only drop a position once the broker confirms it closed.
                    self.managed_positions.pop(oid, None)
                    self.position_store.remove_position(oid)
                    closed += 1
                else:
                    failed_oids.append(oid)
                    logger.error(
                        "Margin flatten close REJECTED for {} {} — retaining under "
                        "management for retry (still open at broker)",
                        pos.direction, pos.symbol,
                    )
            except Exception as exc:
                failed_oids.append(oid)
                logger.error("Margin flatten failed for {}: {}", oid, exc)
        from brain.drawdown_guard import DrawdownMode
        self.drawdown.mode = DrawdownMode.FROZEN
        if failed_oids:
            logger.critical(
                "🚨 MARGIN FLATTEN — {} closed, {} STILL OPEN at broker (retained, "
                "not dropped): {} — risk FROZEN",
                closed, len(failed_oids), failed_oids,
            )
        else:
            logger.critical("🚨 MARGIN FLATTEN COMPLETE — {} positions closed, risk FROZEN", closed)
        try:
            self._persist_guard_state()
        except Exception as exc:
            logger.error("Guard state persist after margin flatten failed: {}", exc)

    def _flatten_account(self, account: str, reason: str) -> None:
        """Flatten (broker-confirmed close) every open position belonging to a
        single account silo, used when that account breaches its hard daily-loss
        flatten cap. Mirrors the margin-flatten safety: a position the broker
        fails to close is RETAINED (never dropped) and re-tried next cycle."""
        closed = 0
        failed_oids: list[str] = []
        for oid, pos in list(self.managed_positions.items()):
            try:
                if self._account_key(pos.symbol) != account:
                    continue
            except Exception:
                continue
            try:
                result = self.platforms.close_trade(oid, pos.platform)
                if result.success:
                    self._record_closed_trade(
                        pos, result.close_price, reason, close_result=result,
                    )
                    # Only drop a position once the broker confirms it closed.
                    self.managed_positions.pop(oid, None)
                    self.position_store.remove_position(oid)
                    closed += 1
                else:
                    failed_oids.append(oid)
                    logger.error(
                        "Account-flatten close REJECTED for {} {} — retained for retry "
                        "(still open at broker)",
                        pos.direction, pos.symbol,
                    )
            except Exception as exc:
                failed_oids.append(oid)
                logger.error("Account-flatten failed for {}: {}", oid, exc)
        if failed_oids:
            logger.critical(
                "🚨 ACCOUNT FLATTEN '{}' — {} closed, {} STILL OPEN at broker (retained): {}",
                account, closed, len(failed_oids), failed_oids,
            )
        else:
            logger.critical(
                "🚨 ACCOUNT FLATTEN '{}' COMPLETE — {} positions closed", account, closed,
            )
        try:
            self._persist_guard_state()
        except Exception as exc:
            logger.error("Guard state persist after account flatten failed: {}", exc)

