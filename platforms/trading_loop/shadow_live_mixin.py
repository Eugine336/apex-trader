"""
APEX TRADER — Live Full-Fidelity Shadow Resolution (Phase 4)

Resolves rejected-setup shadow contracts on the LIVE feed using the SAME
intelligence real trades use — no historical CSVs, no fabricated data.

Each PENDING contract becomes a paper position that is advanced every cycle on
the live bars the loop already fetched and managed by the real engines:
  * TradeManager.update()  — TP1 / SL / breakeven / trailing / stall mechanics
  * scanner.scan_pair()    — fresh re-score on live data
  * SituationEngine + DecisionEngine + RiskGovernor — strategic exits

The ONLY difference from a real trade is that decisions are applied to the
paper position instead of being sent to the broker. When the paper trade hits
its SL/TP/exit the genuine WIN/LOSS/R is written back to the ShadowStore.

Fail-safe: any error on a single shadow is swallowed — the shadow layer can
never affect real trading. The paper TradeManager is entirely separate from the
real one, so paper trades never touch broker reconciliation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from loguru import logger

from decision.actions import Action
from management.trade_manager import TradeStatus, TERMINAL_STATUSES


class ShadowLiveMixin:
    """Advances rejected-setup paper trades on the live feed each cycle."""

    def _advance_shadows(self, now: datetime) -> None:
        """Per-cycle driver: register new shadows, advance active ones one step."""
        store = getattr(self, "_shadow_store", None)
        if store is None or getattr(self, "_shadow_tm", None) is None:
            return
        market = getattr(self, "_last_market_data", None) or {}
        try:
            self._register_new_shadows(store, now)
        except Exception as exc:
            logger.debug("[shadow-live] register failed: {}", exc)

        active = getattr(self, "_active_shadows", {})
        if not active:
            return
        currency_data = {p: f["H1"] for p, f in market.items() if "H1" in f}
        for cid in list(active.keys()):
            try:
                self._advance_one_shadow(cid, market, currency_data, now)
            except Exception as exc:
                logger.debug("[shadow-live] advance {} failed: {}", cid, exc)

    # ── Registration ─────────────────────────────────────────────────────

    def _register_new_shadows(self, store, now: datetime) -> None:
        if len(self._active_shadows) >= self._max_active_shadows:
            return
        now_ms = int(now.timestamp() * 1000)
        min_ts = now_ms - int(self._shadow_max_pending_age_seconds * 1000)
        pending = store.get_pending(limit=self._max_active_shadows * 3)
        from persistence.shadow_resolver import _build_managed_trade

        for c in pending:
            if len(self._active_shadows) >= self._max_active_shadows:
                break
            if c.contract_id in self._active_shadows:
                continue
            if c.ts_utc_ms < min_ts:
                continue  # too old to manage forward; discarded elsewhere
            try:
                trade = _build_managed_trade(c)  # trade_id = "shadow_<cid>"
                self._shadow_tm._trades[trade.trade_id] = trade
            except Exception as exc:
                logger.debug("[shadow-live] build paper trade failed for {}: {}", c.contract_id, exc)
                continue
            direction = "BUY" if str(c.direction).upper() in ("BUY", "LONG") else "SELL"
            pos = SimpleNamespace(
                symbol=c.symbol,
                direction=direction,
                entry_type="SHADOW",
                entry_price=c.entry_price,
                sl=c.stop_loss,
                sl_original=c.stop_loss,
                at_breakeven=False,
                tp1_hit=False,
                trailing=False,
                lots=c.position_size,
                tm_trade_id=trade.trade_id,
                open_time=datetime.fromtimestamp(c.ts_utc_ms / 1000, tz=timezone.utc),
                score=c.score,
                broker_pnl=0.0,
                stake_usd=0.0,
                multiplier=100,
            )
            self._active_shadows[c.contract_id] = pos

    # ── Per-shadow advance ───────────────────────────────────────────────

    def _advance_one_shadow(self, cid: str, market: dict, currency_data: dict, now: datetime) -> None:
        pos = self._active_shadows.get(cid)
        if pos is None:
            return
        trade = self._shadow_tm.get_trade(pos.tm_trade_id)
        if trade is None:
            self._active_shadows.pop(cid, None)
            return

        frames = market.get(pos.symbol)
        if not frames:
            return  # no live data this cycle — try again next cycle
        h4 = frames.get("H4")
        h1 = frames.get("H1")
        m15 = frames.get("M15")
        m5 = frames.get("M5")
        d1 = frames.get("D1")
        m1 = frames.get("M1")
        if m5 is None or h1 is None:
            return

        try:
            tick = self.platforms.get_price(pos.symbol)
            current_price = tick.bid if pos.direction.upper() in ("BUY", "LONG") else tick.ask
        except Exception:
            try:
                current_price = float(m5["close"].iloc[-1])
            except Exception:
                return

        # 1. Tick-level mechanics — the SAME TradeManager real trades use.
        trade = self._shadow_tm.update(
            trade, current_price=current_price, current_df_m5=m5, bar_time=now,
        )
        pos.sl = trade.stop_loss
        pos.at_breakeven = bool(getattr(trade, "breakeven_active", False))
        pos.tp1_hit = bool(getattr(trade, "partial_closed", False))
        pos.trailing = getattr(trade, "trailing_stop", None) is not None

        if trade.status in TERMINAL_STATUSES:
            self._resolve_shadow(cid, trade, now)
            return

        # 2. Strategic management — the SAME decision-engine + governor stack.
        if self._decision_enabled and h4 is not None and m15 is not None:
            try:
                scan_result = self.scanner.scan_pair(
                    pos.symbol, h4, h1, m15, m5, currency_data, now, d1_df=d1,
                )
            except Exception:
                scan_result = SimpleNamespace(
                    score=pos.score, direction=pos.direction, confluences=[],
                )
            hold_minutes = (now - pos.open_time).total_seconds() / 60
            ctx = self._build_trade_context(
                cid, pos, scan_result,
                sa_data={"d1": d1, "h4": h4, "h1": h1, "m1": m1},
                hold_minutes=hold_minutes,
                trade_manager=self._shadow_tm,
            )
            sa = self._situation_engine.assess_open_trade(ctx)
            decision = self._decision_engine.decide_management(ctx, sa)
            if self._risk_governor is not None:
                decision = self._risk_governor.review(decision, ctx, sa)
            self._apply_shadow_decision(pos, trade, decision, current_price, now)
            if trade.status in TERMINAL_STATUSES:
                self._resolve_shadow(cid, trade, now)

    def _apply_shadow_decision(self, pos, trade, decision, current_price: float, now: datetime) -> None:
        """Apply a management decision to the PAPER trade (never the broker)."""
        act = getattr(decision, "action", None)
        if act == Action.CLOSE:
            self._shadow_tm.close_trade(
                trade,
                f"DECISION_ENGINE({str(getattr(decision, 'reason', ''))[:60]})",
                current_price,
                TradeStatus.CLOSED,
                bar_time=now,
            )
        elif act in (Action.TIGHTEN_SL, Action.SET_PROTECTIVE_STOP, Action.MOVE_TO_BREAKEVEN):
            new_sl = getattr(decision, "new_sl", None)
            if new_sl:
                trade.stop_loss = new_sl
                pos.sl = new_sl
                if act == Action.MOVE_TO_BREAKEVEN:
                    trade.breakeven_active = True

    # ── Resolution write-back ────────────────────────────────────────────

    def _resolve_shadow(self, cid: str, trade, now: datetime) -> None:
        from persistence.shadow_resolver import _classify_outcome, _compute_r_multiple
        from persistence.shadow_store import ShadowResolution

        try:
            outcome = _classify_outcome(trade)
            r_mult = _compute_r_multiple(trade)
            res = ShadowResolution(
                outcome=outcome,
                r_multiple=round(r_mult, 4),
                exit_reason=trade.close_reason or "live",
                exit_price=trade.current_price,
                resolution_ts=int(now.timestamp() * 1000),
                resolution_granularity="LIVE",
                bars_replayed=0,
                resolver_meta={
                    "final_status": trade.status.value,
                    "pnl_pips": round(trade.pnl_pips, 2),
                    "mode": "live_full_fidelity",
                },
            )
            self._shadow_store.resolve_contract(cid, res)
            logger.debug(
                "👻 Shadow resolved (live) — {} {} | {} | {:+.2f}R",
                trade.direction, trade.pair, outcome, r_mult,
            )
        except Exception as exc:
            logger.debug("[shadow-live] resolve {} failed: {}", cid, exc)
        finally:
            self._shadow_tm._trades.pop(getattr(trade, "trade_id", ""), None)
            self._active_shadows.pop(cid, None)
