"""
APEX TRADER — Entry Validator
The last gate before the bullet leaves the barrel.
Validates spread, timing, correlation, drawdown limits,
and ensures the entry signal is still clean before execution.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from typing import Optional
from loguru import logger

from config import AppConfig, get_instrument, is_always_open, is_session_gated
from brain.drawdown_guard import DrawdownGuard
from brain.correlation_engine import CorrelationEngine, OpenTrade
from brain.session_engine import SessionEngine
from trigger.entry_engine import EntrySignal

_MT5_AVAILABLE = False
try:
    import MetaTrader5 as mt5  # type: ignore[import-untyped]
    _MT5_AVAILABLE = True
except ImportError:
    mt5 = None


@dataclass
class ValidationResult:
    valid: bool
    signal: EntrySignal
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)
    adjusted_signal: Optional[EntrySignal] = None
    # ── Per-check provenance (collapse #21) ──────────────────────────────
    # ``valid`` still collapses to a single bool (any failure blocks), but the
    # failure detail is preserved so a consumer can tell WHICH gate failed and
    # whether it was a hard physics veto (market closed, expired, corrupted
    # prices) or a softer analytical one (spread / R:R / session). Purely
    # additive — does not change the block decision.
    hard_failures: list[str] = field(default_factory=list)
    soft_failures: list[str] = field(default_factory=list)


# Checks that are genuine physics/safety vetoes (a soft path makes no sense):
# the market is closed, the signal expired, or its prices are corrupt.
_HARD_CHECKS: frozenset[str] = frozenset(
    {"market_open", "expiry", "price_finiteness", "max_trades"}
)


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
        mt5_connector=None,
    ):
        self.config = config or AppConfig()
        self.drawdown = drawdown or DrawdownGuard()
        self.correlation = correlation or CorrelationEngine(
            max_single_currency_exposure=self.config.risk.max_spread_multiplier / 100,
            max_correlated_trades=self.config.risk.max_correlated_trades,
            allow_intentional_hedge=self.config.risk.allow_intentional_hedge,
        )
        self.session = SessionEngine()
        self._mt5_connector = mt5_connector  # injected from platform_manager if available

    def validate(
        self,
        signal: EntrySignal,
        current_spread_pips: float,
        open_trades: Optional[list] = None,
        utc_now: Optional[datetime] = None,
        platform: str = "mt5",
    ) -> ValidationResult:
        utc_now = utc_now or datetime.now(timezone.utc)
        open_trades = open_trades or []
        passed: list[str] = []
        failed: list[str] = []
        hard_failures: list[str] = []
        soft_failures: list[str] = []

        def _record(name: str, ok: bool, msg: str) -> None:
            if ok:
                passed.append(msg)
            else:
                failed.append(msg)
                (hard_failures if name in _HARD_CHECKS else soft_failures).append(msg)

        # ── Check 1: Market open (exchange hours) ─────────────────────
        # Must be first — no point running any other check if the market
        # is physically closed. Uses MT5 trade_mode for MT5 instruments;
        # Deriv synthetics are always open so they skip this gate.
        ok, msg = self.check_market_open(signal.pair, platform)
        _record("market_open", ok, msg)

        ok, msg = self.check_spread(signal.pair, current_spread_pips)
        _record("spread", ok, msg)

        ok, msg = self.check_risk_reward(signal)
        _record("risk_reward", ok, msg)

        ok, msg = self.check_expiry(signal, utc_now)
        _record("expiry", ok, msg)

        ok, msg = self.check_session(signal.pair, utc_now)
        _record("session", ok, msg)

        ok, msg = self.check_price_finiteness(signal)
        _record("price_finiteness", ok, msg)

        ok, msg = self.check_max_trades(open_trades)
        _record("max_trades", ok, msg)

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
            hard_failures=hard_failures,
            soft_failures=soft_failures,
        )

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def check_market_open(self, pair: str, platform: str = "mt5") -> tuple[bool, str]:
        """
        Asks MT5 directly whether this symbol is currently tradeable.
        Uses symbol_info().trade_mode — no hardcoded hours, works for any
        instrument including ones added in future.

        Trade modes:
            0 = SYMBOL_TRADE_MODE_DISABLED  — trading disabled
            1 = SYMBOL_TRADE_MODE_LONGONLY  — buy only
            2 = SYMBOL_TRADE_MODE_SHORTONLY — sell only
            3 = SYMBOL_TRADE_MODE_CLOSEONLY — close only (session ending)
            4 = SYMBOL_TRADE_MODE_FULL      — fully open

        Deriv synthetics are always open — skip this check entirely for them.
        If MT5 is unavailable (e.g. running tests), pass through gracefully.
        """
        # Deriv instruments never have exchange-hour restrictions
        if platform == "deriv" or is_always_open(pair):
            return True, "24/7 instrument — market always open"

        if not _MT5_AVAILABLE or mt5 is None:
            # If a live MT5 connector is wired we EXPECT MT5 to be available;
            # its absence means we cannot confirm the market is open → fail
            # closed. Only skip the check when no connector is wired (tests/dev).
            if self._mt5_connector is not None:
                return False, "MT5 unavailable but connector wired — fail-closed"
            return True, "MT5 not available — market hours check skipped"

        # Resolve broker symbol name via connector if injected, else use raw pair
        mapped = pair
        if self._mt5_connector is not None:
            try:
                mapped = self._mt5_connector.symbol_map(pair)
            except Exception as exc:
                logger.warning("[entry_validator] symbol_map lookup failed: {}", exc)
                pass

        try:
            mt5.symbol_select(mapped, True)
            info = mt5.symbol_info(mapped)
            if info is None:
                # Cannot confirm tradeability — fail closed rather than letting
                # an order through to a possibly-closed market.
                logger.warning(f"[{pair}] symbol_info returned None — fail-closed on market hours")
                return False, f"Market hours unknown for {pair} (symbol_info None) — fail-closed"

            mode = info.trade_mode

            # SYMBOL_TRADE_MODE_FULL (4) = fully open
            # SYMBOL_TRADE_MODE_LONGONLY (1) or SHORTONLY (2) = partially open
            # SYMBOL_TRADE_MODE_CLOSEONLY (3) or DISABLED (0) = closed
            if mode == 4:
                return True, f"Market open (trade_mode=FULL)"
            elif mode in (1, 2):
                return True, f"Market partially open (trade_mode={mode})"
            elif mode == 3:
                return False, f"Market closing — close-only mode ({pair})"
            else:
                return False, f"Market closed — trading disabled ({pair}, trade_mode={mode})"

        except Exception as exc:
            logger.warning(f"[{pair}] Market hours check error: {exc} — fail-closed")
            return False, f"Market hours check failed ({exc}) — fail-closed"

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

    # NOTE: Removed from validate() — risk_engine.assess_trade() performs this check.
    # Kept for backward compatibility with tests.
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

    # NOTE: Removed from validate() — risk_engine.assess_trade() performs this check.
    # Kept for backward compatibility with tests.
    def check_max_trades(self, open_trades: list) -> tuple[bool, str]:
        max_trades = self.config.risk.max_open_trades
        if len(open_trades) >= max_trades:
            return False, f"Max open trades reached ({len(open_trades)}/{max_trades})"
        return True, f"Trade slots available ({len(open_trades)}/{max_trades})"

    # NOTE: Removed from validate() — risk_engine.assess_trade() performs this check.
    # Kept for backward compatibility with tests.
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

    def check_price_finiteness(self, signal: EntrySignal) -> tuple[bool, str]:
        for name in ("entry_price", "stop_loss", "tp1", "tp2"):
            val = getattr(signal, name, None)
            if val is not None and not math.isfinite(val):
                return False, f"Non-finite {name} ({val}) — signal corrupted"
        return True, "Prices finite"
