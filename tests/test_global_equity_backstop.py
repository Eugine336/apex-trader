"""
Test for #7 — multi-broker global accounting.

The global daily/weekly drawdown backstop must measure pooled P&L against TOTAL
portfolio equity (sum of all account balances), not whichever single account
happened to enter last. Per-account caps are handled separately by the silos.
"""

from datetime import datetime, timezone

from risk.account_risk import AccountRiskManager
from risk.risk_engine import RiskEngine


class TestAccountTotalBalance:
    def test_sums_accounts(self):
        am = AccountRiskManager()
        am.update_balance("deriv:1", 5.0)
        am.update_balance("mt5:1", 10.0)
        assert am.total_balance() == 15.0

    def test_empty_is_zero(self):
        assert AccountRiskManager().total_balance() == 0.0


class TestGlobalDrawdownDenominator:
    def test_daily_limit_uses_total_equity_not_account(self):
        # Total portfolio equity = $1000; a single account entering is $10.
        # A pooled -$2 loss is -0.2% of the portfolio (fine) but -20% of the
        # $10 account (which the OLD code would have frozen on).
        e = RiskEngine(starting_balance=1000.0)
        e.pnl_tracker.record(-2.0, False, datetime.now(timezone.utc))
        res = e.assess(
            pair="EURUSD", direction="LONG",
            entry_price=1.10000, stop_loss=1.09800,
            account_balance=10.0,
        )
        # No daily-limit rejection: the portfolio is barely down.
        assert not any(
            ("daily" in r.lower() and "limit" in r.lower()) or "frozen" in r.lower()
            for r in res.rejections
        )

    def test_daily_limit_trips_on_total_equity_breach(self):
        # -$1 on $15 total equity = -6.7% > 5% daily limit → FROZEN.
        e = RiskEngine(starting_balance=15.0)
        e.pnl_tracker.record(-1.0, False, datetime.now(timezone.utc))
        res = e.assess(
            pair="EURUSD", direction="LONG",
            entry_price=1.10000, stop_loss=1.09800,
            account_balance=10.0,
        )
        assert res.approved is False
        assert any("limit" in r.lower() or "frozen" in r.lower() for r in res.rejections)
