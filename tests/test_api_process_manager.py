"""Tests for the per-user process manager and instance environment building."""

import time
from pathlib import Path

import pytest

from api.config import ApiConfig
from api.database import Database
from api.instance_config import build_instance_environment
from api.process_manager import STATUS_STOPPED, ProcessManager

_FAKE_MAIN = "import time\nwhile True:\n    time.sleep(0.2)\n"


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_API_DATA_DIR", str(tmp_path / "api_data"))
    monkeypatch.setenv("APEX_API_JWT_SECRET", "x")
    monkeypatch.setenv("APEX_API_VAULT_KEY", "y")
    return ApiConfig()


@pytest.fixture()
def db(config):
    return Database(config.database_path)


def test_build_instance_environment_isolation(tmp_path):
    api_db = tmp_path / "apex_api.db"
    workdir = tmp_path / "user_1"
    cfg = workdir / "user_config.json"
    env = build_instance_environment(
        user_id=1,
        workdir=workdir,
        config_path=cfg,
        api_db_path=api_db,
        broker_credentials={
            "mt5": {"login": 111, "password": "pw", "server": "Srv"},
            "deriv": {"access_token": "tok", "app_id": "9", "account_type": "demo"},
        },
        base_env={},
    )
    assert env["APEX_USER_ID"] == "1"
    assert env["APEX_DATA_DIR"].endswith("data")
    assert env["APEX_LOG_DIR"].endswith("logs")
    assert env["APEX_TRADE_REPORT_DB"] == str(api_db.resolve())
    assert env["USE_EVENT_DRIVEN"] == "true"
    # MT5 creds serialised into the engine's MT5_BROKERS JSON + legacy vars.
    assert '"login": 111' in env["MT5_BROKERS"]
    assert env["MT5_PASSWORD"] == "pw"
    assert env["MT5_SERVER"] == "Srv"
    # Deriv creds mapped to the engine's env vars.
    assert env["DERIV_ACCESS_TOKEN"] == "tok"
    assert env["DERIV_APP_ID"] == "9"
    # Per-user data/log dirs were created.
    assert (workdir / "data").is_dir()
    assert (workdir / "logs").is_dir()


def _point_to_fake_main(pm: ProcessManager, tmp_path: Path) -> None:
    fake_repo = tmp_path / "fake_repo"
    fake_repo.mkdir(parents=True, exist_ok=True)
    (fake_repo / "main.py").write_text(_FAKE_MAIN, encoding="utf-8")
    pm._repo_root = fake_repo  # noqa: SLF001 — test seam


def test_start_status_stop(config, db, tmp_path):
    pm = ProcessManager(config, db)
    _point_to_fake_main(pm, tmp_path)
    creds = {"mt5": {"login": 1, "password": "p", "server": "s"}}

    status = pm.start_instance(1, creds)
    assert status["alive"] is True
    assert status["pid"]

    # Idempotent: starting again returns the same running instance.
    again = pm.start_instance(1, creds)
    assert again["pid"] == status["pid"]

    stopped = pm.stop_instance(1, user_initiated=True)
    assert stopped["alive"] is False
    assert stopped["status"] == STATUS_STOPPED


def test_start_requires_credentials(config, db, tmp_path):
    pm = ProcessManager(config, db)
    _point_to_fake_main(pm, tmp_path)
    with pytest.raises(ValueError):
        pm.start_instance(1, {})


def test_capacity_limit(config, db, tmp_path, monkeypatch):
    config.max_instances = 1
    pm = ProcessManager(config, db)
    _point_to_fake_main(pm, tmp_path)
    creds = {"mt5": {"login": 1, "password": "p", "server": "s"}}
    pm.start_instance(1, creds)
    try:
        with pytest.raises(RuntimeError):
            pm.start_instance(2, creds)
    finally:
        pm.stop_instance(1, user_initiated=True)


def test_tail_log_returns_lines(config, db, tmp_path):
    pm = ProcessManager(config, db)
    _point_to_fake_main(pm, tmp_path)
    pm.start_instance(1, {"mt5": {"login": 1, "password": "p", "server": "s"}})
    time.sleep(0.3)
    lines = pm.tail_log(1, lines=50)
    pm.stop_instance(1, user_initiated=True)
    # The spawn writes a start banner to the log.
    assert any("instance start" in ln for ln in lines)


def test_reconcile_marks_running_as_stopped(config, db):
    pm = ProcessManager(config, db)
    db.upsert_instance(5, "RUNNING", pid=12345)
    pm.reconcile_on_startup()
    assert db.get_instance(5)["status"] == STATUS_STOPPED


def test_credential_loader_used_for_autorestart(config, db, tmp_path):
    pm = ProcessManager(config, db)
    pm.credential_loader = lambda uid: {"mt5": {"login": uid}}  # type: ignore[attr-defined]
    assert pm._load_credentials(7) == {"mt5": {"login": 7}}  # noqa: SLF001
