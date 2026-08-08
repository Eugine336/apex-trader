"""Tests for the engine→API trade reporting bridge."""

from api.database import Database
from api.trade_reporter import report_trade_close


def test_report_noop_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_TRADE_REPORT_DB", raising=False)
    monkeypatch.delenv("APEX_USER_ID", raising=False)
    assert report_trade_close(symbol="EURUSD", direction="LONG", pnl_dollars=1.0) is False


def test_report_writes_row_when_enabled(tmp_path, monkeypatch):
    db_path = tmp_path / "apex_api.db"
    db = Database(db_path)  # creates schema (incl. trade_history)
    uid = db.create_user("t@u.com", "h")

    monkeypatch.setenv("APEX_TRADE_REPORT_DB", str(db_path))
    monkeypatch.setenv("APEX_USER_ID", str(uid))

    ok = report_trade_close(
        symbol="EURUSD",
        direction="SHORT",
        pnl_dollars=-2.5,
        pnl_pips=-25.0,
        ticket="T123",
        entry_price=1.1,
        exit_price=1.1025,
        exit_reason="stop_loss",
    )
    assert ok is True
    trades = db.list_trades(uid)
    assert len(trades) == 1
    assert trades[0]["symbol"] == "EURUSD"
    assert trades[0]["ticket"] == "T123"
    assert round(trades[0]["pnl"], 2) == -2.5


def test_report_bad_user_id_is_noop(tmp_path, monkeypatch):
    db_path = tmp_path / "apex_api.db"
    Database(db_path)
    monkeypatch.setenv("APEX_TRADE_REPORT_DB", str(db_path))
    monkeypatch.setenv("APEX_USER_ID", "not-an-int")
    assert report_trade_close(symbol="X", direction="LONG", pnl_dollars=1.0) is False
