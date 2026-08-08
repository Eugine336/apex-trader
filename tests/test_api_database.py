"""Tests for the API SQLite data-access layer."""

import pytest

from api.database import Database


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "apex_api.db")


def test_user_crud(db):
    uid = db.create_user("USER@example.com", "hash", is_admin=True)
    assert uid > 0
    by_email = db.get_user_by_email("user@example.com")  # case-insensitive
    assert by_email is not None
    assert by_email["is_admin"] == 1
    by_id = db.get_user_by_id(uid)
    assert by_id["email"] == "user@example.com"
    assert db.count_users() == 1


def test_duplicate_email_rejected(db):
    db.create_user("dup@example.com", "h")
    with pytest.raises(Exception):
        db.create_user("dup@example.com", "h2")


def test_set_password_and_active(db):
    uid = db.create_user("a@b.com", "h1")
    db.set_user_password(uid, "h2")
    assert db.get_user_by_id(uid)["password_hash"] == "h2"
    db.set_user_active(uid, False)
    assert db.get_user_by_id(uid)["is_active"] == 0


def test_broker_credentials_upsert_and_delete(db):
    uid = db.create_user("c@d.com", "h")
    db.upsert_broker_credentials(uid, "mt5", "cipher1", label="primary")
    rows = db.list_broker_credentials(uid)
    assert len(rows) == 1 and rows[0]["encrypted_credentials"] == "cipher1"
    # Upsert replaces (unique per user+broker_type).
    db.upsert_broker_credentials(uid, "mt5", "cipher2")
    assert db.get_broker_credentials(uid, "mt5")["encrypted_credentials"] == "cipher2"
    assert db.delete_broker_credentials(uid, "mt5") is True
    assert db.delete_broker_credentials(uid, "mt5") is False


def test_instance_lifecycle(db):
    uid = db.create_user("e@f.com", "h")
    db.upsert_instance(uid, "RUNNING", pid=999)
    inst = db.get_instance(uid)
    assert inst["status"] == "RUNNING" and inst["pid"] == 999
    db.update_instance_status(uid, "CRASHED", pid=None, last_error="boom", increment_restarts=True)
    inst = db.get_instance(uid)
    assert inst["status"] == "CRASHED" and inst["restarts"] == 1
    db.update_instance_status(uid, "STOPPED")
    assert db.get_instance(uid)["stopped_at"] is not None


def test_trade_history_and_stats(db):
    uid = db.create_user("g@h.com", "h")
    db.insert_trade(uid, {"symbol": "EURUSD", "direction": "LONG", "pnl": 10.0, "closed_at": "2026-01-01T00:00:00+00:00"})
    db.insert_trade(uid, {"symbol": "EURUSD", "direction": "SHORT", "pnl": -4.0, "closed_at": "2026-01-02T00:00:00+00:00"})
    db.insert_trade(uid, {"symbol": "GBPUSD", "direction": "LONG", "pnl": 6.0, "closed_at": "2026-01-03T00:00:00+00:00"})
    stats = db.trade_stats(uid)
    assert stats["total"] == 3
    assert stats["wins"] == 2 and stats["losses"] == 1
    assert round(stats["total_pnl"], 2) == 12.0
    assert stats["win_rate"] == round(2 / 3, 4)
    # Filtering.
    eur = db.list_trades(uid, symbol="EURUSD")
    assert len(eur) == 2
    # Equity curve cumulative + chronological.
    curve = db.equity_curve(uid)
    assert [round(p["equity"], 2) for p in curve] == [10.0, 6.0, 12.0]


def test_user_config_roundtrip(db):
    uid = db.create_user("i@j.com", "h")
    assert db.get_user_config(uid) is None
    db.set_user_config(uid, '{"risk_per_trade_pct": 1.0}')
    assert db.get_user_config(uid) == '{"risk_per_trade_pct": 1.0}'


def test_password_reset_flow(db):
    uid = db.create_user("k@l.com", "h")
    db.create_password_reset("tok", uid, "2099-01-01T00:00:00+00:00")
    rec = db.get_password_reset("tok")
    assert rec["used"] == 0 and rec["user_id"] == uid
    db.mark_password_reset_used("tok")
    assert db.get_password_reset("tok")["used"] == 1
