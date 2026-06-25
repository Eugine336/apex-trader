"""APEX TRADER — Entry Gate (Phase 7).

Pre-entry validation checks extracted from ``trigger/entry_engine.py``
and ``trigger/entry_validator.py``.  Each gate is an independent,
testable function that returns a GateResult.

Gate ordering (fail-fast: cheapest checks first):

    1. instrument_known     — symbol is in the registry
    2. price_finite         — entry/SL/TP are finite numbers
    3. market_open          — exchange is currently tradeable
    4. session_active       — not in DEAD / WEEKEND period
    5. spread_ok            — spread within multiplier of typical
    6. news_clear           — no high-impact news ±15 min
    7. drawdown_ok          — drawdown guard allows trading
    8. score_minimum        — confluence score meets threshold
    9. ev_gate / alignment  — directional check: expected-value gate
                              (default, Phase 4) or the legacy HTF-alignment
                              floor when ``ev_gate_enabled`` is False
   10. risk_reward_ok       — R:R meets minimum requirement
   11. zone_valid           — entry zone has not expired / invalidated

All gates are pure functions of their inputs (no side effects).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from entry.models import EntryConfig, EntryZone, GateResult


class EntryGate:
    """Runs all pre-entry validation checks.  Fail-closed by default."""

    def __init__(
        self,
        config: Optional[EntryConfig] = None,
        gate_tuner: Optional[object] = None,
    ) -> None:
        self._config = config or EntryConfig()
        # Optional shadow-fed GateTuner.  When present it can LOWER the entry
        # score bar within a bounded envelope (offset ∈ [-3, 0]) if the gate's
        # rejected setups keep winning.  The lowered bar never drops below
        # ``watchlist_score`` (enforced in _check_score).  Neutral (None) by
        # default so the live gate is unchanged unless a tuner is wired.
        self._gate_tuner = gate_tuner

    def validate_all(
        self,
        *,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        tp1: float,
        tp2: float,
        score: int,
        current_spread_pips: float,
        zone: Optional[EntryZone] = None,
        alignment: Optional[float] = None,
        posture: str = "",
        long_probability: float = 0.0,
        short_probability: float = 0.0,
        is_instrument_known: bool = True,
        is_market_open: bool = True,
        is_session_active: bool = True,
        is_news_clear: bool = True,
        is_drawdown_ok: bool = True,
        utc_now: Optional[datetime] = None,
    ) -> tuple[bool, list[GateResult]]:
        """Run every gate and return (all_passed, results).

        Every gate runs regardless of earlier failures so the caller
        gets a complete diagnostic.

        The directional gate is the EV gate (``ev_gate_enabled``, default) —
        ``long_probability``/``short_probability`` are the Phase 3 probabilistic
        evidence. When the EV gate is disabled the legacy ``alignment`` floor
        gate runs instead, so callers that only supply ``alignment`` keep their
        prior behaviour.
        """
        utc_now = utc_now or datetime.now(timezone.utc)
        results: list[GateResult] = []

        if getattr(self._config, "ev_gate_enabled", True):
            directional_gate = self._check_ev(
                symbol, direction, entry_price, stop_loss, tp1,
                long_probability, short_probability,
            )
        else:
            directional_gate = self._check_alignment(
                symbol, direction, alignment, posture,
            )

        gates = [
            self._check_instrument_known(symbol, is_instrument_known),
            self._check_price_finite(entry_price, stop_loss, tp1, tp2),
            self._check_market_open(symbol, is_market_open),
            self._check_session_active(symbol, is_session_active),
            self._check_spread(symbol, current_spread_pips),
            self._check_news(symbol, is_news_clear),
            self._check_drawdown(is_drawdown_ok),
            self._check_score(symbol, score),
            directional_gate,
            self._check_risk_reward(entry_price, stop_loss, tp1, tp2, direction),
            self._check_zone_valid(zone, utc_now),
        ]

        results.extend(gates)
        all_passed = all(g.passed for g in results)

        if not all_passed:
            failed = [g for g in results if not g.passed]
            logger.warning(
                "[entry-gate] {} {} REJECTED — {}",
                symbol, direction,
                "; ".join(f"{g.gate_name}: {g.reason}" for g in failed),
            )

        return all_passed, results

    @staticmethod
    def _check_instrument_known(
        symbol: str, known: bool,
    ) -> GateResult:
        if not known:
            return GateResult(False, "instrument_known", f"{symbol} not in instrument registry")
        return GateResult(True, "instrument_known", "OK")

    @staticmethod
    def _check_price_finite(
        entry: float, sl: float, tp1: float, tp2: float,
    ) -> GateResult:
        for name, val in [("entry", entry), ("sl", sl), ("tp1", tp1), ("tp2", tp2)]:
            if not math.isfinite(val):
                return GateResult(False, "price_finite", f"Non-finite {name}={val}")
        return GateResult(True, "price_finite", "All prices finite")

    @staticmethod
    def _check_market_open(symbol: str, is_open: bool) -> GateResult:
        if not is_open:
            return GateResult(False, "market_open", f"{symbol} market closed")
        return GateResult(True, "market_open", "Market open")

    @staticmethod
    def _check_session_active(symbol: str, active: bool) -> GateResult:
        if not active:
            return GateResult(False, "session_active", f"{symbol} session inactive")
        return GateResult(True, "session_active", "Session active")

    def _max_spread_pips(self, symbol: str) -> float:
        """Instrument-aware spread ceiling in pips.

        The ceiling scales with the instrument's own typical spread
        (``max_spread_multiplier × typical_spread_pips``) rather than a single
        hardcoded 5-pip "typical" that is far too tight for indices/metals/
        synthetics and too loose for tight-spread majors. Falls back to the
        legacy 5-pip basis when the instrument (or its typical spread) is
        unknown.
        """
        mult = float(getattr(self._config, "max_spread_multiplier", 3.0))
        typical = 5.0
        try:
            from config import get_instrument

            info = get_instrument(symbol)
            ts = float(getattr(info, "typical_spread_pips", 0.0) or 0.0)
            if ts > 0:
                typical = ts
        except Exception:
            typical = 5.0
        return mult * typical

    def _check_spread(
        self, symbol: str, spread_pips: float,
    ) -> GateResult:
        max_spread = self._max_spread_pips(symbol)
        if spread_pips > max_spread:
            return GateResult(
                False, "spread_ok",
                f"Spread {spread_pips:.1f} > max {max_spread:.1f}",
            )
        return GateResult(True, "spread_ok", f"Spread {spread_pips:.1f} OK")

    @staticmethod
    def _check_news(symbol: str, clear: bool) -> GateResult:
        if not clear:
            return GateResult(False, "news_clear", f"High-impact news near {symbol}")
        return GateResult(True, "news_clear", "No news block")

    @staticmethod
    def _check_drawdown(ok: bool) -> GateResult:
        if not ok:
            return GateResult(False, "drawdown_ok", "Drawdown guard blocking")
        return GateResult(True, "drawdown_ok", "Drawdown clear")

    def _check_score(self, symbol: str, score: int) -> GateResult:
        min_score = self._config.min_entry_score
        # Apply the learned GateTuner offset (bounded, loosening-only) so the
        # live bar reflects what the shadow outcomes have proven — but never
        # below the watchlist floor.  Previously the tuner's offset only
        # reached the legacy backtest engine, never the live EntryGate.
        if self._gate_tuner is not None:
            try:
                offset = float(self._gate_tuner.offset("entry_engine"))
                floor = int(getattr(self._config, "watchlist_score", min_score))
                min_score = max(floor, int(round(min_score + offset)))
            except Exception as exc:
                logger.debug(
                    "[entry-gate] gate-tuner offset unavailable: {}", exc,
                )
        if score < min_score:
            return GateResult(
                False, "score_minimum",
                f"Score {score} < minimum {min_score}",
            )
        return GateResult(True, "score_minimum", f"Score {score} OK")

    def _check_ev(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        tp1: float,
        long_probability: float,
        short_probability: float,
    ) -> GateResult:
        """Expected-value gate (Phase 4 — Opportunity Engine).

        Replaces the alignment-floor gate as the directional check. Instead of
        asking "does the HTF bias support this direction?" (a hard veto that
        blocked every counter-trend idea), it asks "given the probabilistic
        evidence and this zone's reward:risk, is the expected value positive
        enough?".

        ``EV = p_win × R:R − p_loss`` (in R-multiples), where ``p_win`` /
        ``p_loss`` are the Phase 3 long/short probabilities mapped to the trade
        direction. A counter-trend trade (``p_loss > p_win``) must clear a
        higher EV bar (``min_entry_ev + counter_trend_ev_premium``) because it
        fights the predominant flow. The GateTuner can LOWER the EV bar within
        its bounded ``ev_gate`` envelope when its rejected setups keep winning,
        but never below zero.
        """
        risk = abs(entry_price - stop_loss)
        if risk <= 0:
            return GateResult(False, "ev_gate", "Zero risk distance")

        reward = abs(tp1 - entry_price)
        rr_ratio = reward / risk

        if direction.upper() == "LONG":
            p_win = float(long_probability)
            p_loss = float(short_probability)
        else:
            p_win = float(short_probability)
            p_loss = float(long_probability)

        if not (math.isfinite(p_win) and math.isfinite(p_loss) and math.isfinite(rr_ratio)):
            return GateResult(False, "ev_gate", "Non-finite EV inputs")

        entry_ev = p_win * rr_ratio - p_loss

        is_counter = p_loss > p_win
        min_ev = float(getattr(self._config, "min_entry_ev", 0.3))
        if is_counter:
            min_ev += float(getattr(self._config, "counter_trend_ev_premium", 0.1))

        # Learned GateTuner offset (bounded, loosening-only): lower the EV bar
        # when the setups this gate rejected keep winning. Never below 0 (a
        # non-positive-EV trade is never admitted).
        if self._gate_tuner is not None:
            try:
                offset = float(self._gate_tuner.offset("ev_gate"))
                min_ev = max(0.0, min_ev + offset)
            except Exception as exc:
                logger.debug(
                    "[entry-gate] gate-tuner ev_gate offset unavailable: {}", exc,
                )

        label = "counter-trend" if is_counter else "with-trend"
        if entry_ev < min_ev:
            return GateResult(
                False, "ev_gate",
                f"EV {entry_ev:.3f}R < min {min_ev:.3f}R "
                f"({label}, p_win={p_win:.2f} rr={rr_ratio:.2f})",
            )
        return GateResult(
            True, "ev_gate",
            f"EV {entry_ev:.3f}R OK "
            f"({label}, p_win={p_win:.2f} rr={rr_ratio:.2f})",
        )

    def _check_alignment(
        self, symbol: str, direction: str, alignment: Optional[float],
        posture: str = "",
    ) -> GateResult:
        """Reject entries that strongly oppose the higher-timeframe bias.

        ``alignment`` is signed: +1 = HTF fully supports the trade direction,
        -1 = HTF fully opposes. A setup below ``min_htf_alignment`` is closed
        almost immediately by management's HTF structure read, so taking it
        only pays spread twice. ``None`` (no alignment supplied) is permissive
        so callers that cannot compute it keep prior behaviour.

        ``posture`` is the situation read (e.g. ``"MIXED"``). A MIXED posture has
        no clear directional edge, so its alignment bar is RAISED to require
        genuine HTF support (≥ +0.30) rather than merely "not strongly opposed"
        — a low-conviction setup with a weakly positive but split HTF stack (the
        slow-bleed loss pattern) no longer sails through the permissive floor.
        """
        min_align = getattr(self._config, "min_htf_alignment", None)
        if min_align is None or alignment is None:
            return GateResult(True, "alignment", "No alignment floor")
        if not math.isfinite(alignment):
            return GateResult(True, "alignment", "Alignment unavailable")
        # Apply the learned GateTuner offset (bounded, loosening-only) so the
        # HTF-alignment floor reflects what the shadow outcomes have proven:
        # if the counter-HTF setups this gate rejected keep winning, the floor
        # is lowered (more counter-HTF allowed) within the tuner's envelope —
        # but never below the absolute permissive cap (min_htf_alignment_floor),
        # and the tuner can only loosen, never tighten past the configured floor.
        if self._gate_tuner is not None:
            try:
                offset = float(self._gate_tuner.offset("htf_alignment"))
                cap = float(getattr(self._config, "min_htf_alignment_floor", -1.0))
                min_align = max(cap, float(min_align) + offset)
            except Exception as exc:
                logger.debug(
                    "[entry-gate] gate-tuner htf_alignment offset unavailable: {}", exc,
                )
        # MIXED posture: a no-clear-edge setup must show real HTF support, not
        # just escape the permissive floor. Layered ON TOP of the tuner result
        # (max of the two) so a learned loosening can never drop a MIXED entry
        # below the +0.30 conviction bar.
        if str(posture or "").upper() == "MIXED":
            min_align = max(min_align, 0.30)
        if alignment < min_align:
            return GateResult(
                False, "alignment",
                f"Alignment {alignment:.2f} < minimum {min_align:.2f}",
            )
        return GateResult(True, "alignment", f"Alignment {alignment:.2f} OK")

    def _check_risk_reward(
        self,
        entry: float,
        sl: float,
        tp1: float,
        tp2: float,
        direction: str,
    ) -> GateResult:
        risk = abs(entry - sl)
        if risk <= 0:
            return GateResult(False, "risk_reward_ok", "Zero risk distance")
        # Sign validation: an inverted SL/TP (e.g. SL above entry on a LONG)
        # would still yield a positive R:R via abs() and pass the magnitude
        # check. Reject geometrically impossible setups first.
        is_long = direction.upper() == "LONG"
        if is_long and (sl >= entry or tp1 <= entry or tp2 <= entry):
            return GateResult(False, "risk_reward_ok", "Inverted SL/TP for LONG")
        if not is_long and (sl <= entry or tp1 >= entry or tp2 >= entry):
            return GateResult(False, "risk_reward_ok", "Inverted SL/TP for SHORT")
        reward1 = abs(tp1 - entry)
        rr1 = reward1 / risk
        min_rr = getattr(self._config, "min_structural_rr", 1.0)
        if rr1 < min_rr:
            return GateResult(
                False, "risk_reward_ok",
                f"TP1 R:R {rr1:.2f} < {min_rr:.2f}",
            )
        reward2 = abs(tp2 - entry)
        rr2 = reward2 / risk
        if rr2 < min_rr:
            return GateResult(
                False, "risk_reward_ok",
                f"TP2 R:R {rr2:.2f} < min {min_rr:.2f}",
            )
        return GateResult(True, "risk_reward_ok", f"R:R TP1={rr1:.2f} TP2={rr2:.2f}")

    @staticmethod
    def _check_zone_valid(
        zone: Optional[EntryZone], utc_now: datetime,
    ) -> GateResult:
        if zone is None:
            return GateResult(False, "zone_valid", "No entry zone provided")
        if utc_now > zone.expires_at:
            return GateResult(False, "zone_valid", "Entry zone expired")
        return GateResult(True, "zone_valid", "Zone valid")
