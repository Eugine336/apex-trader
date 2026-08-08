"""APEX TRADER — Dashboard Trade History Mixin."""

from dashboard.state_helpers import HelpersMixin


class HistoryMixin(HelpersMixin):
    """get_trade_history()."""

    def get_trade_history(self) -> dict:
        if not self.is_live:
            return {"trades": []}

        balance = self._get_balance()
        rows = self._build_history_rows(balance)
        rows.sort(key=lambda r: r["opened_at"], reverse=True)
        return {"trades": rows}
