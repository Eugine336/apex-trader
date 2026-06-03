"""Recovery and reconciliation methods for TradingLoop."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from loguru import logger

from management.trade_manager import EntrySignal as TMEntrySignal
from platforms.base_connector import OrderResult, CloseResult, PositionInfo
from platforms.trading_loop.positions import ManagedPosition


class RecoveryReconciliationMixin:
    """Mixin providing startup recovery and broker reconciliation."""

    def _perform_startup_recovery(self) -> None:
        """Restore persisted positions and reconcile with the broker.

        Idempotent — safe to call from both ``run()`` and the dashboard
        startup path.  The flag ensures restore+reconcile execute at most
        once per process lifetime.
        """
        if self._recovery_completed:
            return
        self._restore_positions()
        self._reconcile_positions()
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

    def _reconcile_positions(self) -> None:
        """Compare persisted positions with broker's live positions on startup.

        Requires positive confirmation from each position's own platform.
        Positions whose platform did not respond are retained and marked
        for revalidation — never removed on absence of data.
        """
        snap = self.platforms.get_open_positions_snapshot()

        if not snap.confirmed_platforms:
            logger.warning(
                "Startup reconciliation skipped — no platform confirmed "
                "(failed: {})",
                snap.failed_platforms or "none connected",
            )
            return

        broker_by_id: dict[str, PositionInfo] = {
            p.order_id: p for p in snap.positions
        }
        persisted_ids = set(self.managed_positions.keys())
        broker_ids = set(broker_by_id.keys())

        removed_count = 0
        for oid in persisted_ids - broker_ids:
            pos = self.managed_positions[oid]
            if pos.platform in snap.confirmed_platforms:
                logger.info(
                    "📋 RECONCILE — {} {} was closed externally while "
                    "offline — removing",
                    pos.direction,
                    pos.symbol,
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

        for oid in broker_ids - persisted_ids:
            bp = broker_by_id[oid]
            logger.warning(
                "⚠️ RECONCILE — Orphaned position found: {} {} {:.2f} lots — adopting",
                bp.direction,
                bp.symbol,
                bp.lots,
            )
            dummy_order = OrderResult(
                success=True,
                order_id=bp.order_id,
                fill_price=bp.open_price,
                requested_price=bp.open_price,
                slippage_pips=0.0,
                lots=bp.lots,
                symbol=bp.symbol,
                direction=bp.direction,
                sl=bp.sl,
                tp=bp.tp,
                platform=bp.platform,
            )
            managed = ManagedPosition(
                order=dummy_order,
                tp1=bp.tp,
                tp2=0.0,
                score=0,
                regime="UNKNOWN",
                session="UNKNOWN",
                entry_type="ORPHAN_ADOPTED",
            )
            tm_signal = TMEntrySignal(
                pair=bp.symbol,
                direction=bp.direction,
                entry_price=bp.open_price,
                stop_loss=bp.sl,
                tp1=bp.tp,
                tp2=0.0,
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

        for oid in persisted_ids & broker_ids:
            bp = broker_by_id[oid]
            pos = self.managed_positions[oid]
            if pos.revalidation_pending:
                pos.revalidation_pending = False
                pos.unconfirmed_cycles = 0
            if abs(bp.sl - pos.sl) > 1e-8:
                logger.debug("RECONCILE — {} SL updated from broker: {:.5f} → {:.5f}", pos.symbol, pos.sl, bp.sl)
                pos.sl = bp.sl
                self.position_store.update_position(oid, sl=bp.sl)

        adopted = len(broker_ids - persisted_ids)
        logger.info(
            "Reconciliation complete — {} managed, {} on broker, {} adopted, "
            "{} removed, {} unconfirmed (confirmed: {}, failed: {})",
            len(self.managed_positions),
            len(snap.positions),
            adopted,
            removed_count,
            sum(1 for p in self.managed_positions.values() if p.revalidation_pending),
            snap.confirmed_platforms or "none",
            snap.failed_platforms or "none",
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
                is_buy = pos.direction == "BUY"
                fake_close.close_price = tick.bid if is_buy else tick.ask
            except Exception as exc:
                logger.debug("[Reconcile] close-price fetch failed for {} {}, using fallback price: {}", pos.direction, pos.symbol, exc)
                pass
            self._record_closed_trade(
                pos,
                fake_close.close_price,
                "CLOSED_EXTERNALLY",
                close_result=fake_close if real_pnl != 0.0 else None,
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

    def _check_and_reconnect(self) -> None:
        """Non-blocking reconnect check — attempts only when backoff timer allows."""
        for platform in ("mt5", "deriv"):
            if self.platforms.should_attempt_reconnect(platform):
                success = self.platforms.reconnect_platform(platform)
                if success:
                    logger.info("🔄 {} recovered — reconciling positions", platform.upper())
                    try:
                        self._reconcile_positions()
                    except Exception as exc:
                        logger.warning("Post-reconnect reconciliation error: {}", exc)

