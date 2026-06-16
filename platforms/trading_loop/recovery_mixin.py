"""Recovery and reconciliation methods for TradingLoop."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from loguru import logger

from brain.symbol_mapper import resolve_to_internal
from management.exit_cause import ExitCause
from management.trade_manager import EntrySignal as TMEntrySignal
from persistence.position_store import STORE_UNAVAILABLE
from platforms.base_connector import OrderResult, CloseResult, PositionInfo
from platforms.trading_loop.positions import ManagedPosition


class RecoveryReconciliationMixin:
    """Mixin providing startup recovery and broker reconciliation."""

    @staticmethod
    def _validated_adopted_tp1(
        direction: str,
        entry_price: float,
        sl: float,
        broker_tp: float,
    ) -> float:
        """Return a validated TP1 for an adopted orphan position.

        If the broker TP is valid (positive and on the profitable side of
        entry), it is returned unchanged.  Otherwise a reconstructed TP1 is
        computed from entry ± 1.5 × risk when a usable SL exists, or 0.0
        (TP management disabled — PR-1 sentinel guard treats ≤ 0 as
        "no target") when risk cannot be derived.
        """
        is_long = direction.upper() in ("BUY", "LONG")

        broker_tp_valid = (
            isinstance(broker_tp, (int, float))
            and broker_tp > 0
            and (
                (is_long and broker_tp > entry_price)
                or (not is_long and broker_tp < entry_price)
            )
        )
        if broker_tp_valid:
            return float(broker_tp)

        sl_usable = (
            isinstance(sl, (int, float))
            and sl > 0
            and abs(sl - entry_price) > 1e-8
        )
        if not sl_usable:
            return 0.0

        risk = abs(entry_price - sl)
        if is_long:
            return round(entry_price + 1.5 * risk, 8)
        return round(entry_price - 1.5 * risk, 8)

    @staticmethod
    def _reconstructed_adopted_tp2(
        direction: str,
        entry_price: float,
        sl: float,
        tp1: float,
    ) -> float:
        """Return a reconstructed TP2 for an adopted orphan position.

        Derives TP2 as entry ± 2.5 × risk (a further R-multiple beyond TP1)
        on the correct side for direction.  Returns 0.0 only when risk
        cannot be derived (mirrors the TP1 sentinel pattern).
        """
        is_long = direction.upper() in ("BUY", "LONG")

        sl_usable = (
            isinstance(sl, (int, float))
            and sl > 0
            and abs(sl - entry_price) > 1e-8
        )
        if not sl_usable:
            return 0.0

        risk = abs(entry_price - sl)
        if is_long:
            return round(entry_price + 2.5 * risk, 8)
        return round(entry_price - 2.5 * risk, 8)

    def _perform_startup_recovery(self) -> None:
        """Restore persisted positions and reconcile with the broker.

        Idempotent — safe to call from both ``run()`` and the dashboard
        startup path.  The flag ensures restore+reconcile execute at most
        once per process lifetime.
        """
        if self._recovery_completed:
            return
        if self.position_store:
            try:
                state = self.position_store.load_guard_state()
                if state is not None:
                    # v2 payload is composite ({"drawdown":…, "risk_engine":…,
                    # "governor":…}); legacy payload is the flat drawdown dict.
                    dd_state = state.get("drawdown", state)
                    self.drawdown.restore_state(dd_state)
                    re_state = state.get("risk_engine")
                    if re_state and hasattr(self, "risk_engine") and hasattr(self.risk_engine, "restore_state"):
                        try:
                            self.risk_engine.restore_state(re_state)
                        except Exception as exc:
                            logger.debug("RiskEngine state restore failed: {}", exc)
                    gov_state = state.get("governor")
                    gov = getattr(self, "_governor", None)
                    if gov_state and gov is not None and hasattr(gov, "restore_state"):
                        try:
                            gov.restore_state(gov_state)
                            logger.info(
                                "Governor state restored — daily_pnl={:.2f}, halted={}",
                                gov.daily_pnl, gov.daily_trading_halted,
                            )
                        except Exception as exc:
                            logger.debug("Governor state restore failed: {}", exc)
                    ar_state = state.get("account_risk")
                    ar = getattr(self, "_account_risk", None)
                    if ar_state and ar is not None and hasattr(ar, "restore_state"):
                        try:
                            ar.restore_state(ar_state)
                            logger.info("Per-account risk state restored")
                        except Exception as exc:
                            logger.debug("Account risk state restore failed: {}", exc)
                    logger.info(
                        "Drawdown guard restored — mode={}, daily_pnl={}",
                        self.drawdown.mode.value,
                        self.drawdown.daily_pnl_history,
                    )
            except Exception as exc:
                logger.warning("Drawdown guard restore failed — starting fresh: {}", exc)
        self._restore_positions()
        self._reconcile_positions()
        self._reconcile_in_flight_intents()
        self._last_reconcile_time = datetime.now(timezone.utc)
        self._recovery_completed = True
        logger.info("Startup recovery complete — periodic reconcile armed ({}s)", self._reconcile_interval_seconds)

    def _restore_positions(self) -> None:
        """Reload positions persisted before the last shutdown/crash."""
        rows = self.position_store.load_all_positions()
        if not rows:
            logger.info("Position store — no persisted positions to restore")
            return
        for row in rows:
            try:
                dummy_order = OrderResult(
                    success=True,
                    order_id=row["order_id"],
                    fill_price=row["entry_price"],
                    requested_price=row["entry_price"],
                    slippage_pips=0.0,
                    lots=row["lots"],
                    symbol=row["symbol"],
                    direction=row["direction"],
                    sl=row["sl"],
                    tp=row["tp1"],
                    platform=row["platform"],
                )
                pos = ManagedPosition(
                    order=dummy_order,
                    tp1=row["tp1"],
                    tp2=row["tp2"],
                    score=row["score"],
                    regime=row["regime"],
                    session=row["session"],
                    entry_type=row["entry_type"],
                    stake_usd=row["stake_usd"],
                    multiplier=row.get("multiplier", 100),
                    confluences=json.loads(row.get("confluences_json", "[]")),
                )
                pos.open_time = datetime.fromisoformat(row["open_time"])
                pos.tp1_hit = bool(row["tp1_hit"])
                pos.at_breakeven = bool(row["at_breakeven"])
                pos.trailing = bool(row["trailing"])
                pos.tm_trade_id = row["tm_trade_id"]
                pos.last_update = datetime.fromisoformat(row["last_update"])
                pos.initial_risk_dollars = row.get("initial_risk_dollars")
                pos.scale_in_count = int(row.get("scale_in_count", 0) or 0)
                # Restore plan linkage so the close path still logs the plan→
                # outcome pair and scale-in honours the per-trade directive.
                pos.plan_id = row.get("plan_id", "") or ""
                pos.plan_sl_pips = float(row.get("plan_sl_pips", 0.0) or 0.0)
                _psa = row.get("plan_scale_in_allowed")
                pos.plan_scale_in_allowed = None if _psa is None else bool(_psa)

                tm_signal = TMEntrySignal(
                    pair=pos.symbol,
                    direction=pos.direction,
                    entry_price=pos.entry_price,
                    stop_loss=pos.sl,
                    tp1=pos.tp1,
                    tp2=pos.tp2,
                    risk_reward_1=1.0,
                    risk_reward_2=2.0,
                    position_size_lots=pos.lots,
                    score=pos.score,
                )
                tm_trade = self.trade_manager.open_trade(tm_signal)
                if pos.tp1_hit:
                    tm_trade.partial_closed = True
                    tm_trade.breakeven_active = True
                # Restore the TP3 ladder flag so an already-banked TP3 is not
                # re-banked after a restart.
                if row.get("tp3_hit"):
                    tm_trade.tp3_hit = True
                pos.tm_trade_id = tm_trade.trade_id
                self.managed_positions[pos.order_id] = pos
                self._position_scores[pos.order_id] = [pos.score]
                logger.info(
                    "🔄 RESTORED — {} {} | lots={} | SL={:.5f} | tp1_hit={}",
                    pos.direction,
                    pos.symbol,
                    pos.lots,
                    pos.sl,
                    pos.tp1_hit,
                )
            except Exception as exc:
                logger.error("Failed to restore position {}: {}", row.get("order_id"), exc)
        logger.info("Position store — {} positions restored from disk", len(self.managed_positions))

    def _reconcile_positions(self) -> bool:
        """Compare persisted positions with broker's live positions on startup.

        Requires positive confirmation from each position's own platform.
        Positions whose platform did not respond are retained and marked
        for revalidation — never removed on absence of data.

        Returns ``True`` only when at least one platform was confirmed (a
        genuine reconcile). Returns ``False`` when no platform responded, so
        callers can keep "last *successful* reconcile" accurate instead of
        treating an unreachable-broker skip as a success.
        """
        snap = self.platforms.get_open_positions_snapshot()

        if not snap.confirmed_platforms:
            logger.warning(
                "Startup reconciliation skipped — no platform confirmed "
                "(failed: {})",
                snap.failed_platforms or "none connected",
            )
            return False

        broker_by_id: dict[str, PositionInfo] = {
            p.order_id: p for p in snap.positions
        }
        persisted_ids = set(self.managed_positions.keys())
        broker_ids = set(broker_by_id.keys())

        removed_count = 0
        for oid in persisted_ids - broker_ids:
            try:
                pos = self.managed_positions[oid]
                if pos.platform in snap.confirmed_platforms:
                    logger.info(
                        "📋 RECONCILE — {} {} was closed externally while "
                        "offline — removing",
                        pos.direction,
                        pos.symbol,
                    )
                    deal_close_info = None
                    close_price = pos.entry_price
                    realized_pnl = None
                    raw_reason_code = None
                    raw_comment = None

                    try:
                        deal_close_info = self.platforms.get_deal_close_info(oid, pos.platform)
                    except Exception as exc:
                        logger.debug(
                            "[Reconcile] deal-close info fetch failed for {} {} ({}): {}",
                            pos.direction, pos.symbol, oid, exc,
                        )

                    if deal_close_info is not None:
                        realized_pnl = deal_close_info.pnl
                        raw_reason_code = deal_close_info.raw_reason_code
                        raw_comment = deal_close_info.raw_comment
                        if deal_close_info.close_price is not None:
                            close_price = float(deal_close_info.close_price)

                    if realized_pnl is None:
                        try:
                            realized_pnl = self.platforms.get_realized_pnl(oid, pos.platform)
                        except Exception as exc:
                            logger.debug(
                                "[Reconcile] realized-pnl fetch failed for {} {} ({}): {}",
                                pos.direction, pos.symbol, oid, exc,
                            )

                    if close_price == pos.entry_price:
                        try:
                            tick = self.platforms.get_price(pos.symbol)
                            is_buy = pos.direction.upper() in ("BUY", "LONG")
                            close_price = tick.bid if is_buy else tick.ask
                        except Exception as exc:
                            logger.debug(
                                "[Reconcile] close-price fetch failed for {} {} ({}), using entry fallback: {}",
                                pos.direction, pos.symbol, oid, exc,
                            )

                    close_result = None
                    if realized_pnl is not None:
                        close_result = CloseResult(
                            success=True,
                            order_id=oid,
                            close_price=close_price,
                            lots_closed=pos.lots,
                            pnl=float(realized_pnl),
                            platform=pos.platform,
                        )

                    try:
                        self._record_closed_trade(
                            pos,
                            close_price,
                            "CLOSED_WHILE_OFFLINE",
                            close_result=close_result,
                            exit_reason_source="broker_history",
                            raw_broker_reason=raw_reason_code,
                            raw_broker_comment=raw_comment,
                            exit_cause=ExitCause.BROKER_SIDE,
                        )
                    except Exception as exc:
                        logger.error(
                            "[Reconcile] failed to journal offline close for {} {} ({}): {}",
                            pos.direction, pos.symbol, oid, exc,
                        )
                    self.managed_positions.pop(oid, None)
                    self.position_store.remove_position(oid)
                    removed_count += 1
                else:
                    pos.revalidation_pending = True
                    pos.unconfirmed_cycles += 1
                    logger.warning(
                        "[Reconcile] Cannot confirm {} {} on {} at startup — "
                        "retaining under management, marked for revalidation",
                        pos.direction, pos.symbol, pos.platform,
                    )
            except Exception as exc:
                logger.warning("[RECONCILE_SKIP] Failed to process persisted position {}: {}", oid, exc)

        adopted_count = 0
        for oid in broker_ids - persisted_ids:
            bp = broker_by_id[oid]

            if bp.lots <= 0:
                logger.warning(
                    "[RECONCILE_SKIP] Orphan {} {} has {:.2f} lots (zero/negative) — skipping adoption",
                    bp.direction, bp.symbol, bp.lots,
                )
                continue

            try:
                internal_symbol = resolve_to_internal(bp.symbol)
                if internal_symbol != bp.symbol:
                    logger.info(
                        "[Reconcile] Resolved broker symbol {} → internal {}",
                        bp.symbol, internal_symbol,
                    )

                validated_tp1 = self._validated_adopted_tp1(
                    bp.direction, bp.open_price, bp.sl, bp.tp,
                )
                if validated_tp1 != bp.tp:
                    logger.warning(
                        "[RECONCILE_TP] {} {} broker TP {:.5f} invalid for entry {:.5f}"
                        " — using reconstructed TP1 {:.5f}",
                        bp.direction, bp.symbol, bp.tp, bp.open_price,
                        validated_tp1,
                    )

                logger.warning(
                    "⚠️ RECONCILE — Orphaned position found: {} {} {:.2f} lots — adopting (internal: {})",
                    bp.direction,
                    bp.symbol,
                    bp.lots,
                    internal_symbol,
                )
                dummy_order = OrderResult(
                    success=True,
                    order_id=bp.order_id,
                    fill_price=bp.open_price,
                    requested_price=bp.open_price,
                    slippage_pips=0.0,
                    lots=bp.lots,
                    symbol=internal_symbol,
                    direction=bp.direction,
                    sl=bp.sl,
                    tp=validated_tp1,
                    platform=bp.platform,
                )
                managed = ManagedPosition(
                    order=dummy_order,
                    tp1=validated_tp1,
                    tp2=self._reconstructed_adopted_tp2(
                        bp.direction, bp.open_price, bp.sl, validated_tp1,
                    ),
                    score=0,
                    regime="UNKNOWN",
                    session="UNKNOWN",
                    entry_type="ORPHAN_ADOPTED",
                )
                tm_signal = TMEntrySignal(
                    pair=internal_symbol,
                    direction=bp.direction,
                    entry_price=bp.open_price,
                    stop_loss=bp.sl,
                    tp1=validated_tp1,
                    tp2=self._reconstructed_adopted_tp2(
                        bp.direction, bp.open_price, bp.sl, validated_tp1,
                    ),
                    risk_reward_1=1.0,
                    risk_reward_2=1.0,
                    position_size_lots=bp.lots,
                    score=0,
                )
                tm_trade = self.trade_manager.open_trade(tm_signal)
                managed.tm_trade_id = tm_trade.trade_id
                self.managed_positions[oid] = managed
                self._position_scores[oid] = [managed.score]
                self.position_store.save_position(managed)
                adopted_count += 1
            except Exception as exc:
                logger.warning(
                    "[RECONCILE_SKIP] Failed to adopt orphan {} {} ({}) — skipping: {}",
                    bp.direction, bp.symbol, oid, exc,
                )

        for oid in persisted_ids & broker_ids:
            try:
                bp = broker_by_id[oid]
                pos = self.managed_positions[oid]
                if pos.revalidation_pending:
                    pos.revalidation_pending = False
                    pos.unconfirmed_cycles = 0
                if abs(bp.sl - pos.sl) > 1e-8:
                    logger.debug("RECONCILE — {} SL updated from broker: {:.5f} → {:.5f}", pos.symbol, pos.sl, bp.sl)
                    pos.sl = bp.sl
                    self.position_store.update_position(oid, sl=bp.sl)
            except Exception as exc:
                logger.warning("[RECONCILE_SKIP] Failed to reconcile existing position {}: {}", oid, exc)

        logger.info(
            "Reconciliation complete — {} managed, {} on broker, {} adopted, "
            "{} removed, {} unconfirmed (confirmed: {}, failed: {})",
            len(self.managed_positions),
            len(snap.positions),
            adopted_count,
            removed_count,
            sum(1 for p in self.managed_positions.values() if p.revalidation_pending),
            snap.confirmed_platforms or "none",
            snap.failed_platforms or "none",
        )
        return True

    def _reconcile_in_flight_intents(self) -> None:
        """Resolve or cancel unresolved in-flight intents from a previous crash."""
        if not self.position_store:
            return
        pending = self.position_store.get_all_pending_in_flight()
        if pending is STORE_UNAVAILABLE:
            logger.warning(
                "[recovery] in-flight intent store unavailable — skipping reconciliation (fail-closed)"
            )
            return
        if not pending:
            logger.debug("[recovery] no pending in-flight intents to reconcile")
            self.position_store.cleanup_stale_in_flight()
            return

        managed_ids = set(self.managed_positions.keys())
        resolved = 0
        cancelled = 0
        for intent in pending:
            idem_key = intent.get("idempotency_key", "")
            symbol = intent.get("symbol", "")
            direction = intent.get("direction", "")
            if not idem_key:
                continue
            try:
                broker_match = self.platforms.find_order_by_idem_key(idem_key)
                if broker_match is not None:
                    order_id = str(
                        getattr(broker_match, "ticket", None)
                        or getattr(broker_match, "order", None)
                        or getattr(broker_match, "contract_id", "")
                        or broker_match
                    )
                    self.position_store.resolve_in_flight(idem_key, order_id)
                    resolved += 1
                    if order_id and order_id not in managed_ids:
                        logger.warning(
                            "⚠️ IN-FLIGHT RESOLVED — {} {} idem_key={} filled as {} "
                            "but not in managed positions — broker reconciliation will adopt",
                            direction, symbol, idem_key, order_id,
                        )
                    else:
                        logger.info(
                            "📋 IN-FLIGHT RESOLVED — {} {} idem_key={} → order {}",
                            direction, symbol, idem_key, order_id,
                        )
                else:
                    self.position_store.cancel_in_flight(idem_key)
                    cancelled += 1
                    logger.info(
                        "📋 IN-FLIGHT CANCELLED — {} {} idem_key={} (no broker trace found)",
                        direction, symbol, idem_key,
                    )
            except Exception as exc:
                logger.warning(
                    "[recovery] in-flight reconciliation failed for idem_key={}: {}",
                    idem_key, exc,
                )
        self.position_store.cleanup_stale_in_flight()
        logger.info(
            "In-flight reconciliation complete — {} resolved, {} cancelled",
            resolved, cancelled,
        )

    # ── Auto-reconnect ─────────────────────────────────────────────────

    def _reconcile_externally_closed(self, to_remove: list[str]) -> None:
        """Drop managed positions that no longer exist at the broker.

        Requires positive confirmation from the position's own platform.
        If the platform failed to respond, the position is retained and
        marked for revalidation — never auto-closed on absence of data.
        """
        if not self.managed_positions:
            return
        snap = self.platforms.get_open_positions_snapshot()
        broker_ids = {p.order_id for p in snap.positions}
        broker_pnl = {p.order_id: p.pnl for p in snap.positions}
        max_unconfirmed = self.config.risk.reconcile_max_unconfirmed_cycles

        for oid, pos in list(self.managed_positions.items()):
            if oid in broker_ids:
                if pos.revalidation_pending:
                    pos.revalidation_pending = False
                    pos.unconfirmed_cycles = 0
                continue

            if pos.platform not in snap.confirmed_platforms:
                pos.revalidation_pending = True
                pos.unconfirmed_cycles += 1
                if pos.unconfirmed_cycles >= max_unconfirmed:
                    logger.critical(
                        "[Reconcile] CRITICAL — {} {} on {} unverifiable for {} cycles "
                        "— retaining under management, requires human review",
                        pos.direction, pos.symbol, pos.platform,
                        pos.unconfirmed_cycles,
                    )
                else:
                    logger.warning(
                        "[Reconcile] Cannot confirm {} {} on {} — retaining under "
                        "management (cycle {}/{})",
                        pos.direction, pos.symbol, pos.platform,
                        pos.unconfirmed_cycles, max_unconfirmed,
                    )
                continue

            real_pnl = broker_pnl.get(oid, 0.0)
            fake_close = CloseResult(
                success=True,
                order_id=oid,
                close_price=pos.entry_price,
                lots_closed=pos.lots,
                pnl=real_pnl,
                platform=pos.platform,
            )
            try:
                tick = self.platforms.get_price(pos.symbol)
                is_buy = pos.direction.upper() in ("BUY", "LONG")
                fake_close.close_price = tick.bid if is_buy else tick.ask
            except Exception as exc:
                logger.debug("[Reconcile] close-price fetch failed for {} {}, using fallback price: {}", pos.direction, pos.symbol, exc)
                pass
            self._record_closed_trade(
                pos,
                fake_close.close_price,
                "CLOSED_EXTERNALLY",
                close_result=fake_close if real_pnl != 0.0 else None,
                exit_cause=ExitCause.BROKER_SIDE,
            )
            to_remove.append(oid)
            self._add_warning(
                "warning",
                f"{pos.direction} {pos.symbol} closed externally by broker",
                symbol=pos.symbol,
            )
            logger.info(
                "📋 LIVE RECONCILE — {} {} closed externally — removed",
                pos.direction,
                pos.symbol,
            )

        if self.position_store:
            try:
                self.position_store.cleanup_stale_in_flight()
            except Exception as exc:
                logger.debug("[reconcile] stale in-flight cleanup failed: {}", exc)

    def _check_and_reconnect(self) -> None:
        """Non-blocking reconnect check — attempts only when backoff timer allows."""
        try:
            self.platforms.check_connections()
        except Exception as exc:
            logger.error("Connection probe failed — continuing cycle: {}", exc)

        for platform in ("mt5", "deriv"):
            if self.platforms.should_attempt_reconnect(platform):
                success = self.platforms.reconnect_platform(platform)
                if success:
                    logger.info("🔄 {} recovered — reconciling positions", platform.upper())
                    try:
                        self._reconcile_positions()
                    except Exception as exc:
                        logger.warning("Post-reconnect reconciliation error: {}", exc)

