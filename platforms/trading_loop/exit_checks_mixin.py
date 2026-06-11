"""Exit-check and in-trade management methods for TradingLoop."""
from __future__ import annotations

from datetime import datetime

from loguru import logger

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from platforms.trading_loop.positions import ManagedPosition


class ExitChecksMixin:
    """Mixin providing in-trade exit checks and active management."""

    def _check_invalidation(
        self,
        oid: str,
        pos: ManagedPosition,
        scan_result,
        now: datetime,
        opposing_score_boost: int = 0,
    ) -> None:
        """
        Exit early if the market is now showing a strong setup AGAINST our position.
        A real trader sees a bearish engulfing form against their long and cuts it —
        they don't wait for SL to get hit.
        """
        cfg = self.config.risk
        is_long = pos.direction == "BUY"
        result_direction = scan_result.direction  # "LONG", "SHORT", or "NEUTRAL"

        # Score too low overall — market has lost conviction on any direction
        if scan_result.score < cfg.invalidation_score_threshold:
            tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
            pnl_pips = tm_trade.pnl_pips if tm_trade else 0.0
            # Only exit on low score if trade is not already in profit
            if pnl_pips <= 0:
                result = self.platforms.close_trade(oid, pos.platform)
                if result.success:
                    logger.info(
                        "🔴 INVALIDATION EXIT (low score) — {} {} | score={} | pnl={:.1f}pip",
                        pos.direction, pos.symbol, scan_result.score, pnl_pips,
                    )
                    self._record_closed_trade(
                        pos, result.close_price,
                        f"INVALIDATION_LOW_SCORE({scan_result.score})",
                        close_result=result,
                    )
                    self.managed_positions.pop(oid, None)
                    self.position_store.remove_position(oid)
                    self._position_scores.pop(oid, None)
                else:
                    logger.warning(
                        "🔴 INVALIDATION close FAILED — {} {} oid={} | position retained: {}",
                        pos.direction, pos.symbol, oid, getattr(result, "error", "unknown"),
                    )
                return

        # Strong opposing signal — market has flipped
        opposing = (
            (is_long and result_direction == "SHORT")
            or (not is_long and result_direction == "LONG")
        )
        effective_opposing_score = min(100, int(scan_result.score + max(0, opposing_score_boost)))
        if opposing and effective_opposing_score >= cfg.opposing_signal_threshold:
            tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
            pnl_pips = tm_trade.pnl_pips if tm_trade else 0.0
            result = self.platforms.close_trade(oid, pos.platform)
            if result.success:
                logger.info(
                    "🔴 INVALIDATION EXIT (opposing signal) — {} {} | opposing={} score={} (ctx+{}) | pnl={:.1f}pip",
                    pos.direction, pos.symbol, result_direction, scan_result.score, max(0, opposing_score_boost), pnl_pips,
                )
                self._record_closed_trade(
                    pos, result.close_price,
                    f"INVALIDATION_OPPOSING({result_direction}@{effective_opposing_score})",
                    close_result=result,
                )
                self.managed_positions.pop(oid, None)
                self.position_store.remove_position(oid)
                self._position_scores.pop(oid, None)
            else:
                logger.warning(
                    "🔴 INVALIDATION (opposing) close FAILED — {} {} oid={} | position retained: {}",
                    pos.direction, pos.symbol, oid, getattr(result, "error", "unknown"),
                )

    def _check_conviction_collapse(
        self, oid: str, pos: ManagedPosition, now: datetime,
    ) -> None:
        """
        Exit if score has been declining consistently for N consecutive cycles.
        A falling score means the market conditions that justified the trade
        are dissolving — a real trader feels this and starts getting out.
        """
        cfg = self.config.risk
        scores = self._position_scores.get(oid, [])
        n = cfg.conviction_decline_cycles
        if len(scores) < n:
            return  # not enough history yet

        recent = scores[-n:]
        # Check if every consecutive pair is declining by at least min_drop
        is_declining = all(
            recent[i] - recent[i + 1] >= cfg.conviction_decline_min_drop
            for i in range(len(recent) - 1)
        )
        if not is_declining:
            return

        tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
        pnl_pips = tm_trade.pnl_pips if tm_trade else 0.0

        # Only exit on conviction collapse if trade is not in significant profit
        # — if we're well in profit, let the trailing stop handle it
        if pnl_pips > 20:
            return

        result = self.platforms.close_trade(oid, pos.platform)
        if result.success:
            logger.info(
                "📉 CONVICTION COLLAPSE EXIT — {} {} | scores={} | pnl={:.1f}pip",
                pos.direction, pos.symbol, recent, pnl_pips,
            )
            self._record_closed_trade(
                pos, result.close_price,
                f"CONVICTION_COLLAPSE(scores:{recent[0]}→{recent[-1]})",
                close_result=result,
            )
            self.managed_positions.pop(oid, None)
            self.position_store.remove_position(oid)
            self._position_scores.pop(oid, None)
        else:
            logger.warning(
                "📉 CONVICTION COLLAPSE close FAILED — {} {} oid={} | position retained: {}",
                pos.direction, pos.symbol, oid, getattr(result, "error", "unknown"),
            )

    def _check_htf_candle_close(
        self,
        oid: str,
        pos: ManagedPosition,
        h1_df,
        now: datetime,
    ) -> None:
        """
        On every new H1 candle close, check if the candle closed against our
        trade direction. A bearish H1 close on a long trade means the higher
        timeframe is rejecting the move — a real trader reassesses immediately.
        """
        if h1_df is None or len(h1_df) < 3:
            return

        # Get the most recently CLOSED H1 candle (index -2, since -1 is forming)
        last_closed = h1_df.iloc[-2]
        candle_time = last_closed.name if hasattr(last_closed, 'name') else None

        if candle_time is None:
            return

        # Only act once per H1 close
        last_seen = self._position_last_h1_close.get(oid)
        if last_seen is not None and candle_time <= last_seen:
            return
        self._position_last_h1_close[oid] = candle_time

        # Check candle direction
        candle_open = float(last_closed.get("open", 0))
        candle_close = float(last_closed.get("close", 0))
        if candle_open == 0 or candle_close == 0:
            return

        is_long = pos.direction == "BUY"
        candle_bearish = candle_close < candle_open
        candle_bullish = candle_close > candle_open

        # Candle body size as a sanity filter — ignore tiny doji candles
        candle_body = abs(candle_close - candle_open)
        candle_range = float(last_closed.get("high", candle_close)) - float(last_closed.get("low", candle_open))
        if candle_range > 0 and (candle_body / candle_range) < 0.3:
            return  # doji — no directional conviction

        opposing_close = (is_long and candle_bearish) or (not is_long and candle_bullish)
        if not opposing_close:
            return

        tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
        if tm_trade is None:
            return

        pnl_pips = tm_trade.pnl_pips
        # Don't exit a trade that's well in profit just because of one H1 candle
        if pnl_pips > 30:
            return

        # If trade is in loss or marginal — exit on HTF rejection
        result = self.platforms.close_trade(oid, pos.platform)
        if result.success:
            direction_str = "BEARISH" if candle_bearish else "BULLISH"
            logger.info(
                "📊 HTF EXIT — {} {} | H1 {} candle close against trade | pnl={:.1f}pip",
                pos.direction, pos.symbol, direction_str, pnl_pips,
            )
            self._record_closed_trade(
                pos, result.close_price,
                f"HTF_H1_{direction_str}_CLOSE",
                close_result=result,
            )
            self.managed_positions.pop(oid, None)
            self.position_store.remove_position(oid)
            self._position_scores.pop(oid, None)
            self._position_last_h1_close.pop(oid, None)
        else:
            logger.warning(
                "📊 HTF EXIT close FAILED — {} {} oid={} | position retained: {}",
                pos.direction, pos.symbol, oid, getattr(result, "error", "unknown"),
            )

    def _apply_dynamic_sl_tightening(self, oid: str, pos: ManagedPosition) -> None:
        """
        Beyond breakeven, progressively lock in profit as the trade develops.
        A trader manually moves their SL higher/lower as price moves in their
        favour — this does it automatically and executes the real modify call.

        Tightening logic:
          - Only activates after breakeven is set
          - Triggers when profit exceeds dynamic_sl_tighten_at_r (default 2R)
          - New SL = current_price - (original_risk * tighten_ratio)
          - Only ever moves SL in profit direction — never backwards
        """
        cfg = self.config.risk
        if not cfg.dynamic_sl_tightening_enabled:
            return

        tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
        if tm_trade is None or not tm_trade.breakeven_active:
            return

        is_long = pos.direction == "BUY"
        try:
            tick = self.platforms.get_price(pos.symbol)
            current = tick.bid if is_long else tick.ask
        except Exception as exc:
            logger.warning("[management] tick fetch for partial close failed, aborting: {}", exc)
            return

        original_risk = abs(pos.entry_price - pos.sl_original) if hasattr(pos, 'sl_original') else abs(pos.entry_price - tm_trade.stop_loss)
        if original_risk < 1e-8:
            return

        if is_long:
            profit_r = (current - pos.entry_price) / original_risk
        else:
            profit_r = (pos.entry_price - current) / original_risk

        if profit_r < cfg.dynamic_sl_tighten_at_r:
            return

        # Calculate tightened SL
        tighten_distance = original_risk * cfg.dynamic_sl_tighten_ratio
        if is_long:
            new_sl = current - tighten_distance
            if new_sl <= pos.sl:
                return  # no improvement
        else:
            new_sl = current + tighten_distance
            if new_sl >= pos.sl:
                return  # no improvement

        new_sl = round(new_sl, 5)
        success = self.platforms.modify_trade(oid, pos.platform, new_sl=new_sl)
        if success:
            old_sl = pos.sl
            pos.sl = new_sl
            tm_trade.stop_loss = new_sl
            self.position_store.update_position(oid, sl=new_sl)
            logger.info(
                "📈 DYNAMIC SL TIGHTEN — {} {} | {:.5f} → {:.5f} | {:.1f}R profit locked",
                pos.direction, pos.symbol, old_sl, new_sl, profit_r,
            )
        else:
            logger.warning(
                "📈 DYNAMIC SL TIGHTEN FAILED — {} {} oid={} | SL move to {:.5f} did NOT land, still at SL={:.5f}",
                pos.direction, pos.symbol, oid, new_sl, pos.sl,
            )

    def _check_news_exit(self, now: datetime) -> None:
        """
        Close or tighten open trades before high-impact news events.
        A real trader checks their economic calendar before every news event
        and manages their exposure accordingly.
        """
        cfg = self.config.risk
        if not cfg.news_exit_enabled:
            return
        if not self.managed_positions:
            return

        try:
            open_pairs = [pos.symbol for pos in self.managed_positions.values()]
            news_status = self.news_guard.check(open_pairs, now)
        except Exception as exc:
            logger.warning("[management] news guard check failed, skipping news exit: {}", exc)
            return

        if not hasattr(news_status, 'upcoming_events'):
            return

        for event in getattr(news_status, 'upcoming_events', []):
            minutes_until = getattr(event, 'minutes_until', None)
            affected_pairs = getattr(event, 'affected_pairs', [])
            impact = getattr(event, 'impact', 'LOW')

            if impact not in ('HIGH', 'MEDIUM'):
                continue
            if minutes_until is None or minutes_until > cfg.news_exit_minutes_before:
                continue

            for oid, pos in list(self.managed_positions.items()):
                if pos.symbol not in affected_pairs:
                    continue
                if oid in self._news_exit_protected:
                    continue

                if cfg.news_exit_mode == "close":
                    result = self.platforms.close_trade(oid, pos.platform)
                    if result.success:
                        logger.info(
                            "📰 NEWS EXIT — {} {} | {} in {:.0f}min | closed @ {:.5f}",
                            pos.direction, pos.symbol, getattr(event, 'name', 'event'),
                            minutes_until, result.close_price,
                        )
                        self._record_closed_trade(
                            pos, result.close_price,
                            f"NEWS_EXIT({getattr(event, 'name', 'event')})",
                            close_result=result,
                        )
                        self.managed_positions.pop(oid, None)
                        self.position_store.remove_position(oid)
                        self._news_exit_protected.discard(oid)
                    else:
                        logger.error(
                            "📰 NEWS EXIT close FAILED — {} {} oid={} | {} in {:.0f}min | "
                            "position retained, UNPROTECTED from news: {}",
                            pos.direction, pos.symbol, oid,
                            getattr(event, 'name', 'event'), minutes_until,
                            getattr(result, "error", "unknown"),
                        )

                elif cfg.news_exit_mode == "tighten":
                    # Move SL to breakeven to protect position
                    tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                    if tm_trade is None:
                        continue
                    be_level = pos.entry_price
                    success = self.platforms.modify_trade(oid, pos.platform, new_sl=be_level)
                    if success:
                        pos.sl = be_level
                        tm_trade.stop_loss = be_level
                        self._news_exit_protected.add(oid)
                        self.position_store.update_position(oid, sl=be_level)
                        logger.info(
                            "📰 NEWS TIGHTEN — {} {} | SL→entry {:.5f} | {} in {:.0f}min",
                            pos.direction, pos.symbol, be_level,
                            getattr(event, 'name', 'event'), minutes_until,
                        )
                    else:
                        logger.error(
                            "📰 NEWS TIGHTEN FAILED — {} {} oid={} | SL move to {:.5f} did NOT land, "
                            "still at SL={:.5f} | {} in {:.0f}min",
                            pos.direction, pos.symbol, oid, be_level, pos.sl,
                            getattr(event, 'name', 'event'), minutes_until,
                        )

    def _check_session_close(self, now: datetime) -> None:
        """
        Manage open trades as sessions end.

        Real traders:
          - Close index trades before the exchange closes for the day
          - Reduce or exit trades entering the dead zone (00:00-02:00 UTC)
          - Don't hold GER40 into the Xetra close at 20:00 UTC
        """
        cfg = self.config.risk
        if not cfg.session_close_enabled:
            return

        utc_hour = now.hour
        utc_minute = now.minute

        # Index session close windows (UTC)
        index_close_windows = {
            "HK50":  (8, 0),    # HKEX closes 08:00 UTC
            "JP225": (6, 30),   # Osaka closes 06:30 UTC
            "AUS200":(6, 0),    # ASX closes 06:00 UTC
            "GER40": (20, 0),   # Xetra closes 20:00 UTC
            "FRA40": (20, 0),   # Euronext closes 20:00 UTC
            "UK100": (16, 30),  # LSE closes 16:30 UTC
            "US30":  (21, 0),   # CME equity closes 21:00 UTC
            "US100": (21, 0),
            "US500": (21, 0),
        }

        for oid, pos in list(self.managed_positions.items()):
            symbol = pos.symbol

            # Check index session close
            close_time = index_close_windows.get(symbol)
            if close_time:
                close_h, close_m = close_time
                # Minutes until close
                close_total = close_h * 60 + close_m
                now_total = utc_hour * 60 + utc_minute
                diff = close_total - now_total
                # Within buffer window before close
                if 0 < diff <= cfg.index_close_buffer_minutes:
                    result = self.platforms.close_trade(oid, pos.platform)
                    if result.success:
                        tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                        pnl = tm_trade.pnl_pips if tm_trade else 0.0
                        logger.info(
                            "🕐 SESSION CLOSE EXIT — {} {} | exchange closes in {}min | pnl={:.1f}pip",
                            pos.direction, symbol, diff, pnl,
                        )
                        self._record_closed_trade(
                            pos, result.close_price,
                            f"SESSION_CLOSE({symbol})",
                            close_result=result,
                        )
                        self.managed_positions.pop(oid, None)
                        self.position_store.remove_position(oid)
                        self._position_scores.pop(oid, None)
                    else:
                        logger.error(
                            "🕐 SESSION CLOSE close FAILED — {} {} oid={} | exchange closes in {}min | "
                            "position retained, UNPROTECTED from session close: {}",
                            pos.direction, symbol, oid, diff,
                            getattr(result, "error", "unknown"),
                        )
                    continue

            # Dead zone management for forex (00:00-02:00 UTC)
            if cfg.dead_zone_management and (pos.symbol.find("USD") >= 0 or pos.symbol.find("JPY") >= 0):
                in_dead_zone = (utc_hour == 0 or (utc_hour == 1 and utc_minute <= 59))
                if in_dead_zone:
                    tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                    if tm_trade and not tm_trade.breakeven_active:
                        # Move to breakeven during dead zone — don't hold unprotected
                        from management.partial_close import PartialCloseCalculator
                        pip_size = tm_trade.pip_size if hasattr(tm_trade, 'pip_size') else 0.0001
                        direction_norm = "LONG" if pos.direction.upper() in ("BUY", "LONG") else "SHORT"
                        be_level = PartialCloseCalculator.calculate_breakeven_level(
                            pos.entry_price, direction_norm, 2.0, pip_size,
                        )
                        success = self.platforms.modify_trade(oid, pos.platform, new_sl=be_level)
                        if success:
                            pos.sl = be_level
                            tm_trade.stop_loss = be_level
                            tm_trade.breakeven_active = True
                            self.position_store.update_position(oid, sl=be_level, at_breakeven=True)
                            logger.info(
                                "🌙 DEAD ZONE PROTECTION — {} {} | SL→BE {:.5f}",
                                pos.direction, symbol, be_level,
                            )
                        else:
                            logger.error(
                                "🌙 DEAD ZONE BE FAILED — {} {} oid={} | SL move to {:.5f} did NOT land, "
                                "still at SL={:.5f}",
                                pos.direction, symbol, oid, be_level, pos.sl,
                            )

    def _check_spread_deterioration(self) -> None:
        """
        Monitor spread quality on open positions.
        If spread widens beyond N× normal (e.g. ahead of news, broker issues,
        thin liquidity), tighten SL to protect against a spike close-out.
        """
        cfg = self.config.risk
        if not cfg.spread_monitor_enabled:
            return

        for oid, pos in list(self.managed_positions.items()):
            try:
                tick = self.platforms.get_price(pos.symbol)
                if not hasattr(tick, 'spread') or tick.spread is None:
                    continue

                # Get typical spread for this instrument from execution monitor
                typical_spread = self.execution_monitor.get_typical_spread(pos.symbol)
                if typical_spread is None or typical_spread < 1e-8:
                    continue

                current_spread = tick.spread
                spread_ratio = current_spread / typical_spread

                if spread_ratio >= cfg.spread_deterioration_multiplier:
                    tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
                    if tm_trade is None or tm_trade.breakeven_active:
                        continue

                    # Tighten SL to breakeven as protection
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
                        self.position_store.update_position(oid, sl=be_level, at_breakeven=True)
                        logger.warning(
                            "📊 SPREAD DETERIORATION — {} {} | spread {:.1f}× normal | SL→BE",
                            pos.direction, pos.symbol, spread_ratio,
                        )
                    else:
                        logger.error(
                            "📊 SPREAD DETERIORATION BE FAILED — {} {} oid={} | SL move to {:.5f} did NOT land, "
                            "still at SL={:.5f} | spread {:.1f}× normal",
                            pos.direction, pos.symbol, oid, be_level, pos.sl, spread_ratio,
                        )
            except Exception as exc:
                logger.debug("Spread monitor error for {}: {}", pos.symbol, exc)

    def _check_opportunity_cost_exit(
        self, oid: str, pos: ManagedPosition, now: datetime,
    ) -> None:
        """Opportunity-cost exit (F4).

        Fires ONLY when all position slots are full AND a materially
        better candidate was rejected this cycle AND this position is
        stagnant/flat.  Differentiated from the existing stall exit
        (which is absolute time-based and ignores portfolio contention)
        by being contention-gated and relative (compares entry score
        against the foregone candidate's score).

        Modes:
          "off"    — inert, early return.
          "shadow" — logs [F4 SHADOW] WOULD-fire, never closes.
          "active" — logs + closes the position via the soft-close
                     idiom (close_trade → _record_closed_trade →
                     managed_positions.pop → position_store.remove).
        """
        cfg = self.config.risk
        if cfg.opportunity_cost_exit_mode == "off":
            return

        if len(self.managed_positions) < cfg.max_open_trades:
            return
        blocked = self._last_slot_blocked_candidate
        if blocked is None:
            return

        hold_minutes = (now - pos.open_time).total_seconds() / 60
        if hold_minutes < cfg.opportunity_cost_min_hold_minutes:
            return

        tm_trade = self.trade_manager.get_trade(pos.tm_trade_id)
        if tm_trade is None:
            return
        if tm_trade.pnl_pips > cfg.opportunity_cost_max_pnl_pips:
            return
        if tm_trade.partial_closed:
            return

        score_delta = blocked["score"] - pos.score
        if score_delta < cfg.opportunity_cost_score_margin:
            return

        logger.info(
            "[F4 SHADOW] opportunity-cost exit WOULD fire — "
            "{} {} (entry_score={}, pnl={:.1f}pip, held={:.0f}min) "
            "← blocked {} {} (score={}, delta=+{})",
            pos.direction, pos.symbol, pos.score,
            tm_trade.pnl_pips, hold_minutes,
            blocked["direction"], blocked["pair"],
            blocked["score"], score_delta,
        )
        if cfg.opportunity_cost_exit_mode == "active":
            result = self.platforms.close_trade(oid, pos.platform)
            if result.success:
                logger.warning(
                    "[F4] OPPORTUNITY-COST EXIT — {} {} closed @ {:.5f} "
                    "(entry_score={}, pnl={:.1f}pip, held={:.0f}min "
                    "← blocked {} {} score={}, delta=+{})",
                    pos.direction, pos.symbol, result.close_price,
                    pos.score, tm_trade.pnl_pips, hold_minutes,
                    blocked["direction"], blocked["pair"],
                    blocked["score"], score_delta,
                )
                self._record_closed_trade(
                    pos, result.close_price,
                    f"OPPORTUNITY_COST(blocked={blocked['pair']})",
                    close_result=result,
                )
                self.managed_positions.pop(oid, None)
                self.position_store.remove_position(oid)
            else:
                logger.error(
                    "[F4] opportunity-cost close FAILED for {} {} — position retained: {}",
                    pos.direction, pos.symbol, getattr(result, "error", "unknown"),
                )

