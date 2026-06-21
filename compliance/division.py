"""APEX TRADER — Compliance Division.

A single, cohesive permit layer.  It answers ONLY one question:

    *Is this trade allowed?*  →  APPROVED | REJECTED

Compliance does **not** size positions, estimate profitability, weigh
expected value, or form any opinion on whether a trade is *wise* — those are
the Portfolio (capital) and Learning (edge) Divisions' responsibilities.

The only vetoes here are the physically necessary ones:

  1. market_open          — the market is open for this symbol
  2. broker_available     — the broker connection is alive
  3. news_clear           — no high-impact news embargo
  4. spread_ok            — the live spread is not abnormally wide
  5. duplicate            — no existing position in this pair+direction
  6. daily_loss           — the daily-loss halt has not tripped
  7. heat_ok              — per-account capital-at-risk is under the block cap
  8. drawdown_not_frozen  — the DrawdownGuard is not FROZEN
  9. max_positions        — the hard simultaneous-position cap is not reached
 10. portfolio_risk_state — the portfolio is not in DEFENSIVE/REDUCING/EMERGENCY

Design rules:

  * **Fail-CLOSED.**  Any exception raised while evaluating a check REJECTS the
    trade (a safety gate that silently no-ops on a bug cannot be trusted).
  * **No short-circuit.**  Every check runs even after the first failure so the
    caller gets the complete set of rejection reasons.
  * **Single source of truth for daily loss.**  The daily-loss permit is
    derived exclusively from :class:`AccountRiskManager` (per-account, realised
    + unrealised).  The PortfolioGovernor no longer competes as a second
    daily-loss authority in the live permit path (it keeps its tally for the
    dashboard only).
  * **Missing subsystem = disabled check (passes).**  A subsystem wired as
    ``None`` disables its check rather than blocking everything — matching the
    rest of the system's "optional subsystem" contract.  This is distinct from
    an *exception inside an active check*, which fails closed.
"""

from __future__ import annotations

from typing import Callable, Optional

from loguru import logger

from compliance.models import ComplianceAccount, ComplianceBook, ComplianceCandidate
from compliance.verdict import CheckOutcome, ComplianceVerdict


class ComplianceDivision:
    """Pure permit layer — APPROVED | REJECTED, fail-closed."""

    def __init__(
        self,
        *,
        drawdown_guard: Optional[object] = None,
        portfolio_risk_sm: Optional[object] = None,
        account_risk: Optional[object] = None,
        news_guard: Optional[object] = None,
        spread_monitor: Optional[object] = None,
        is_market_open: Optional[Callable[[str], bool]] = None,
        is_broker_available: Optional[Callable[[str], bool]] = None,
        get_spread_pips: Optional[Callable[[str], float]] = None,
        max_open_positions: int = 8,
    ) -> None:
        self._drawdown_guard = drawdown_guard
        self._portfolio_risk_sm = portfolio_risk_sm
        self._account_risk = account_risk
        self._news_guard = news_guard
        self._spread_monitor = spread_monitor
        self._is_market_open = is_market_open
        self._is_broker_available = is_broker_available
        self._get_spread_pips = get_spread_pips
        self._max_open_positions = int(max_open_positions)

    def bind_runtime(
        self,
        *,
        is_market_open: Optional[Callable[[str], bool]] = None,
        is_broker_available: Optional[Callable[[str], bool]] = None,
        get_spread_pips: Optional[Callable[[str], float]] = None,
        spread_monitor: Optional[object] = None,
    ) -> None:
        """Inject the broker/platform-bound callables after construction.

        ``SystemContext.create`` builds the division with the subsystem
        references it owns; the event-driven bootstrap (which holds the
        PlatformManager and the broker-truth helpers) binds the
        platform-dependent callables here.  Only non-None arguments overwrite
        existing wiring, so partial binding is safe.
        """
        if is_market_open is not None:
            self._is_market_open = is_market_open
        if is_broker_available is not None:
            self._is_broker_available = is_broker_available
        if get_spread_pips is not None:
            self._get_spread_pips = get_spread_pips
        if spread_monitor is not None:
            self._spread_monitor = spread_monitor

    # ── Public API ───────────────────────────────────────────────────────

    def permit(
        self,
        candidate: ComplianceCandidate,
        book: ComplianceBook,
        account: ComplianceAccount,
    ) -> ComplianceVerdict:
        """Run every permit check and return APPROVED | REJECTED.

        All checks run (no short-circuit) so every rejection reason is
        collected.  Any check that raises is treated as a REJECTION
        (fail-closed).
        """
        checks = (
            ("market_open", self._check_market_open),
            ("broker_available", self._check_broker_available),
            ("news_clear", self._check_news),
            ("spread_ok", self._check_spread),
            ("duplicate", self._check_duplicate),
            ("daily_loss", self._check_daily_loss),
            ("heat_ok", self._check_heat),
            ("drawdown_not_frozen", self._check_drawdown_frozen),
            ("max_positions", self._check_max_positions),
            ("portfolio_risk_state", self._check_portfolio_risk_state),
        )

        outcomes: list[CheckOutcome] = []
        for name, fn in checks:
            outcomes.append(self._run(name, fn, candidate, book, account))

        verdict = ComplianceVerdict.from_outcomes(outcomes)
        if verdict.rejected:
            logger.warning(
                "[compliance] {} {} REJECTED — {}",
                candidate.symbol,
                candidate.direction,
                "; ".join(verdict.reasons),
            )
        return verdict

    # ── Fail-closed wrapper ───────────────────────────────────────────────

    @staticmethod
    def _run(
        name: str,
        fn: Callable[
            [ComplianceCandidate, ComplianceBook, ComplianceAccount], CheckOutcome
        ],
        candidate: ComplianceCandidate,
        book: ComplianceBook,
        account: ComplianceAccount,
    ) -> CheckOutcome:
        try:
            return fn(candidate, book, account)
        except Exception as exc:  # noqa: BLE001 — any failure = fail-closed REJECT
            logger.warning(
                "[compliance] {} check errored for {} {} — failing CLOSED: {}",
                name,
                candidate.symbol,
                candidate.direction,
                exc,
            )
            return CheckOutcome(name, False, f"{name} check error (fail-closed): {exc}")

    # ── Individual checks ─────────────────────────────────────────────────

    def _check_market_open(
        self, c: ComplianceCandidate, _b: ComplianceBook, _a: ComplianceAccount
    ) -> CheckOutcome:
        if self._is_market_open is None:
            return CheckOutcome("market_open", True, "disabled (no market-open source)")
        if self._is_market_open(c.symbol):
            return CheckOutcome("market_open", True, "market open")
        return CheckOutcome("market_open", False, f"{c.symbol} market closed")

    def _check_broker_available(
        self, c: ComplianceCandidate, _b: ComplianceBook, _a: ComplianceAccount
    ) -> CheckOutcome:
        if self._is_broker_available is None:
            return CheckOutcome(
                "broker_available", True, "disabled (no broker-health source)"
            )
        if self._is_broker_available(c.symbol):
            return CheckOutcome("broker_available", True, "broker connected")
        return CheckOutcome(
            "broker_available", False, f"{c.symbol} broker unavailable / disconnected"
        )

    def _check_news(
        self, c: ComplianceCandidate, _b: ComplianceBook, _a: ComplianceAccount
    ) -> CheckOutcome:
        if self._news_guard is None:
            return CheckOutcome("news_clear", True, "disabled (no news guard)")
        status = self._news_guard.check([c.symbol])
        if getattr(status, "is_clear", True):
            return CheckOutcome("news_clear", True, "no news embargo")
        msg = getattr(status, "warning_message", "") or "high-impact news embargo"
        return CheckOutcome("news_clear", False, msg)

    def _check_spread(
        self, c: ComplianceCandidate, _b: ComplianceBook, _a: ComplianceAccount
    ) -> CheckOutcome:
        if self._spread_monitor is None or self._get_spread_pips is None:
            return CheckOutcome("spread_ok", True, "disabled (no spread source)")
        cur_spread = float(self._get_spread_pips(c.symbol))
        safe, why = self._spread_monitor.is_spread_safe(c.symbol, cur_spread)
        if safe:
            return CheckOutcome("spread_ok", True, why)
        return CheckOutcome("spread_ok", False, why)

    def _check_duplicate(
        self, c: ComplianceCandidate, b: ComplianceBook, _a: ComplianceAccount
    ) -> CheckOutcome:
        sym = c.symbol.upper()
        direction = c.direction.upper()
        for pos in b.open_positions:
            psym = str(getattr(pos, "symbol", "") or getattr(pos, "pair", "")).upper()
            pdir = str(getattr(pos, "direction", "")).upper()
            if psym == sym and pdir == direction:
                return CheckOutcome(
                    "duplicate",
                    False,
                    f"already hold {direction} {sym} (duplicate pair+direction)",
                )
        return CheckOutcome("duplicate", True, "no duplicate position")

    def _check_daily_loss(
        self, c: ComplianceCandidate, _b: ComplianceBook, a: ComplianceAccount
    ) -> CheckOutcome:
        # Single authoritative daily-loss source: the per-account silo
        # (realised + unrealised, with hysteresis).  The PortfolioGovernor no
        # longer competes here (V7 — one source of truth).
        if self._account_risk is None:
            return CheckOutcome("daily_loss", True, "disabled (no account-risk silo)")
        if self._account_risk.daily_loss_halted(a.account_key):
            pct = self._account_risk.combined_pnl_pct(a.account_key)
            return CheckOutcome(
                "daily_loss",
                False,
                f"account {a.account_key} daily-loss halt ({pct:.2f}% incl. open)",
            )
        return CheckOutcome("daily_loss", True, "daily loss within cap")

    def _check_heat(
        self, c: ComplianceCandidate, _b: ComplianceBook, a: ComplianceAccount
    ) -> CheckOutcome:
        if self._account_risk is None:
            return CheckOutcome("heat_ok", True, "disabled (no account-risk silo)")
        if self._account_risk.heat_blocked(a.account_key):
            heat = self._account_risk.heat(a.account_key)
            return CheckOutcome(
                "heat_ok",
                False,
                f"account {a.account_key} heat {heat:.2f}% at/over block cap",
            )
        return CheckOutcome("heat_ok", True, "heat under block cap")

    def _check_drawdown_frozen(
        self, _c: ComplianceCandidate, _b: ComplianceBook, _a: ComplianceAccount
    ) -> CheckOutcome:
        if self._drawdown_guard is None:
            return CheckOutcome(
                "drawdown_not_frozen", True, "disabled (no drawdown guard)"
            )
        # Import inside the check so the module stays importable without the
        # brain package present (tests / minimal environments).
        from brain.drawdown_guard import DrawdownMode

        status = self._drawdown_guard.get_status()
        if status.mode == DrawdownMode.FROZEN.value:
            return CheckOutcome(
                "drawdown_not_frozen",
                False,
                "DrawdownGuard FROZEN (daily loss limit hit)",
            )
        return CheckOutcome("drawdown_not_frozen", True, f"drawdown mode={status.mode}")

    def _check_max_positions(
        self, _c: ComplianceCandidate, b: ComplianceBook, _a: ComplianceAccount
    ) -> CheckOutcome:
        open_count = len(b.open_positions)
        if open_count >= self._max_open_positions:
            return CheckOutcome(
                "max_positions",
                False,
                f"max open positions reached ({open_count}/{self._max_open_positions})",
            )
        return CheckOutcome(
            "max_positions",
            True,
            f"{open_count}/{self._max_open_positions} open",
        )

    def _check_portfolio_risk_state(
        self, _c: ComplianceCandidate, _b: ComplianceBook, _a: ComplianceAccount
    ) -> CheckOutcome:
        if self._portfolio_risk_sm is None:
            return CheckOutcome(
                "portfolio_risk_state", True, "disabled (no portfolio risk SM)"
            )
        from risk.portfolio_risk_state import PortfolioRiskState

        state = self._portfolio_risk_sm.state
        if state in (
            PortfolioRiskState.DEFENSIVE,
            PortfolioRiskState.REDUCING,
            PortfolioRiskState.EMERGENCY,
        ):
            return CheckOutcome(
                "portfolio_risk_state",
                False,
                f"portfolio risk state={state.name} (entries frozen)",
            )
        return CheckOutcome("portfolio_risk_state", True, f"state={state.name}")
