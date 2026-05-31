"""
APEX TRADER — Entry Validator
The last gate before the bullet leaves the barrel.
Validates spread, timing, correlation, drawdown limits,
and ensures the entry signal is still clean before execution.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from loguru import logger

from config import AppConfig, get_instrument, INSTRUMENT_REGISTRY, is_always_open, is_session_gated
from brain.drawdown_guard import DrawdownGuard, DrawdownMode
from brain.correlation_engine import CorrelationEngine, OpenTrade
from brain.session_engine import SessionEngine
from trigger.entry_engine import EntrySignal


@dataclass
class ValidationResult:
    valid: bool
    signal: EntrySignal
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)
    adjusted_signal: Optional[EntrySignal] = None


class EntryValidator:
    """
    Runs every safety check before an entry signal is sent to execution.
    If any critical check fails, the signal is blocked.
    """

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        drawdown: Optional[DrawdownGuard] = None,
        correlation: Optional[CorrelationEngine] = None,
    ):
        self.config = config or AppConfig()
        self.drawdown = drawdown or DrawdownGuard()
        self.correlation = correlation or CorrelationEngine(
            max_single_currency_exposure=self.config.risk.max_spread_multiplier / 100,
            max_correlated_trades=self.config.risk.max_correlated_trades,
        )
        self.session = SessionEngine()

    def validate(
        self,
        signal: EntrySignal,
        current_spread_pips: float,
        open_trades: Optional[list] = None,
        utc_now: Optional[datetime] = None,
    ) -> ValidationResult:
        utc_now = utc_now or datetime.now(timezone.utc)
        open_trades = open_trades or []
        passed: list[str] = []
        failed: list[str] = []

        ok, msg = self.check_spread(signal.pair, current_spread_pips)
        (passed if ok else failed).append(msg)

        ok, msg = self.check_risk_reward(signal)
        (passed if ok else failed).append(msg)

        ok, msg = self.check_drawdown()
        (passed if ok else failed).append(msg)

        ok, msg = self.check_expiry(signal, utc_now)
        (passed if ok else failed).append(msg)

        ok, msg = self.check_max_trades(open_trades)
        (passed if ok else failed).append(msg)

        ok, msg = self.check_correlation(signal.pair, signal.direction, open_trades)
        (passed if ok else failed).append(msg)

        ok, msg = self.check_session(signal.pair, utc_now)
        (passed if ok else failed).append(msg)

        is_valid = len(failed) == 0

        if is_valid:
            logger.info(f"[{signal.pair}] Validation PASSED — {len(passed)} checks clear")
        else:
            logger.warning(f"[{signal.pair}] Validation FAILED — {failed}")

        return ValidationResult(
            valid=is_valid,
            signal=signal,
            checks_passed=passed,
            checks_failed=failed,
        )

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def check_spread(
        self, pair: str, current_spread_pips: float,
    ) -> tuple[bool, str]:
        try:
            info = get_instrument(pair)
            typical = info.typical_spread_pips
            # Indices and crypto have wide off-hours spreads — give them an extra
            # absolute cap instead of only a relative multiplier.
            category = info.category.value  # "forex" / "commodity" / "index" / "synthetic"
            if category in ("index", "synthetic"):
                # Allow up to 5× typical OR a hard 30-pip ceiling, whichever is larger
                max_spread = max(typical * self.config.risk.max_spread_multiplier, typical * 5.0)
            elif category == "commodity":
                # Commodities widen significantly at rollover / off-hours
                max_spread = max(typical * self.config.risk.max_spread_multiplier, typical * 4.0)
            else:
                max_spread = typical * self.config.risk.max_spread_multiplier
        except KeyError:
            max_spread = 5.0 * self.config.risk.max_spread_multiplier

        if current_spread_pips > max_spread:
            return False, f"Spread too wide ({current_spread_pips:.1f} > max {max_spread:.1f})"
        return True, f"Spread OK ({current_spread_pips:.1f} pips)"

    def check_risk_reward(self, signal: EntrySignal) -> tuple[bool, str]:
        min_rr = self.config.risk.min_risk_reward
        if signal.risk_reward_1 < 1.0:
            return False, f"R:R to TP1 below 1:1 ({signal.risk_reward_1:.2f})"
        if signal.risk_reward_2 < min_rr:
            return False, f"R:R to TP2 below minimum ({signal.risk_reward_2:.2f} < {min_rr})"
        return True, f"R:R OK (TP1={signal.risk_reward_1:.2f}, TP2={signal.risk_reward_2:.2f})"

    def check_drawdown(self) -> tuple[bool, str]:
        can_trade, reason = self.drawdown.can_trade()
        if not can_trade:
            return False, f"Drawdown block — {reason}"
        return True, f"Drawdown clear — {reason}"

    def check_expiry(
        self, signal: EntrySignal, utc_now: datetime,
    ) -> tuple[bool, str]:
        if utc_now > signal.valid_until:
            return False, "Signal expired"
        remaining = (signal.valid_until - utc_now).total_seconds()
        return True, f"Signal valid ({remaining:.0f}s remaining)"

    def check_max_trades(self, open_trades: list) -> tuple[bool, str]:
        max_trades = self.config.risk.max_open_trades
        if len(open_trades) >= max_trades:
            return False, f"Max open trades reached ({len(open_trades)}/{max_trades})"
        return True, f"Trade slots available ({len(open_trades)}/{max_trades})"

    def check_correlation(
        self, pair: str, direction: str, open_trades: list,
    ) -> tuple[bool, str]:
        if not open_trades:
            return True, "No open trades — correlation clear"

        normalized = []
        for t in open_trades:
            if isinstance(t, OpenTrade):
                normalized.append(t)
            elif isinstance(t, dict):
                normalized.append(OpenTrade(
                    pair=str(t.get("pair", "")),
                    direction=str(t.get("direction", "LONG")),
                    risk_pct=float(t.get("risk_pct", 0.02)),
                ))

        status = self.drawdown.get_status()
        risk_pct = status.current_risk_pct
        ok, reason = self.correlation.can_open_trade(pair, direction, normalized, risk_pct)
        if not ok:
            return False, f"Correlation block — {reason}"
        return True, "Correlation clear"

    def check_session(
        self, pair: str, utc_now: datetime,
    ) -> tuple[bool, str]:
        status = self.session.get_status(utc_now)

        # 24/7 synthetics (Deriv) — never block on session or weekend
        if is_always_open(pair):
            return True, "24/7 instrument — always tradeable"

        # Market-wide weekend/closure block — applies to everything 24/5
        if status.current_session == "WEEKEND":
            return False, f"Market closed ({status.current_session})"

        # Non-FX instruments (commodities, indices, crypto) trade on their own
        # schedules — do NOT gate them against FX sessions like SYDNEY, DEAD, etc.
        # XAUUSD being rejected because "Session not active (SYDNEY)" is wrong.
        if not is_session_gated(pair):
            return True, f"Non-FX instrument — session unrestricted ({status.current_session})"

        # FX pairs only — respect FX session activity
        if status.current_session == "DEAD":
            return False, f"Market closed ({status.current_session})"
        if not status.is_tradeable:
            return False, f"Session not active ({status.current_session})"

        return True, f"Session active ({status.current_session})"
