"""APEX TRADER — Per-user process manager (Option A).

Spawns, supervises, and tears down one isolated APEX trading process per user.
Each instance:

* runs the untouched ``main.py`` as a subprocess,
* has its own working directory under ``<api_data>/instances/user_<id>/``,
* receives its broker credentials + isolated data/log dirs purely via the
  environment (see :func:`api.instance_config.build_instance_environment`),
* writes stdout/stderr to a per-user log file (``logs/instance.log``),
* is auto-restarted on crash with a bounded backoff schedule.

A background monitor thread reaps dead processes, restarts crashed instances
that the user did not explicitly stop, and keeps the database status in sync.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from api.config import ApiConfig
from api.database import Database
from api.instance_config import build_instance_environment, write_user_config

# Instance lifecycle states (mirrored into the DB ``status`` column).
STATUS_STOPPED = "STOPPED"
STATUS_RUNNING = "RUNNING"
STATUS_STARTING = "STARTING"
STATUS_CRASHED = "CRASHED"
STATUS_STOPPING = "STOPPING"

# Tail buffer size for the in-memory log view, in lines.
_LOG_TAIL_MAX = 2000


class _Instance:
    """Bookkeeping for a single user's running subprocess."""

    __slots__ = (
        "user_id",
        "process",
        "workdir",
        "log_path",
        "log_handle",
        "started_at",
        "restart_count",
        "next_backoff_idx",
        "user_stopped",
        "last_exit_at",
        "dashboard_port",
    )

    def __init__(self, user_id: int, workdir: Path, log_path: Path) -> None:
        self.user_id = user_id
        self.process: Optional[subprocess.Popen] = None
        self.workdir = workdir
        self.log_path = log_path
        self.log_handle = None
        self.started_at: float = 0.0
        self.restart_count: int = 0
        self.next_backoff_idx: int = 0
        self.user_stopped: bool = False
        self.last_exit_at: float = 0.0
        # Loopback port the instance's read-only dashboard API listens on, used
        # by the API control plane to proxy live engine-state panels. Allocated
        # per spawn so a restarted instance never collides with a stale binding.
        self.dashboard_port: Optional[int] = None


class ProcessManager:
    """Manage the lifecycle of per-user APEX trading subprocesses."""

    def __init__(self, config: ApiConfig, db: Database) -> None:
        self._config = config
        self._db = db
        self._instances: dict[int, _Instance] = {}
        self._lock = threading.RLock()
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()
        self._repo_root = Path(__file__).resolve().parent.parent
        self._created_at = time.time()

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start_monitor(self) -> None:
        """Start the background supervisor thread (idempotent)."""
        with self._lock:
            if self._monitor_thread and self._monitor_thread.is_alive():
                return
            self._monitor_stop.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, name="apex-instance-monitor", daemon=True
            )
            self._monitor_thread.start()
            logger.info("[proc-mgr] instance monitor started")

    def shutdown(self) -> None:
        """Stop the monitor and terminate all running instances."""
        self._monitor_stop.set()
        with self._lock:
            user_ids = list(self._instances.keys())
        for uid in user_ids:
            try:
                self.stop_instance(uid, user_initiated=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[proc-mgr] shutdown stop failed for {}: {}", uid, exc)
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)

    # ── start ─────────────────────────────────────────────────────────────
    def start_instance(
        self, user_id: int, broker_credentials: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """Launch (or no-op if already running) the user's trading instance.

        *broker_credentials* maps broker_type → decrypted credentials.
        """
        with self._lock:
            existing = self._instances.get(user_id)
            if existing and self._is_alive(existing):
                return self.instance_status(user_id)

            if not broker_credentials:
                raise ValueError("no broker credentials configured for this user")

            running = sum(1 for i in self._instances.values() if self._is_alive(i))
            if running >= self._config.max_instances:
                raise RuntimeError(
                    f"instance capacity reached ({self._config.max_instances})"
                )

            workdir = self._config.user_workdir(user_id)
            workdir.mkdir(parents=True, exist_ok=True)
            log_path = workdir / "logs" / "instance.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)

            inst = self._instances.get(user_id) or _Instance(
                user_id, workdir, log_path
            )
            inst.workdir = workdir
            inst.log_path = log_path
            inst.user_stopped = False

            self._spawn(inst, broker_credentials)
            self._instances[user_id] = inst

        # Surface an immediate startup crash (bad import, instant config error)
        # right at the Start call instead of silently flipping to CRASHED and
        # entering an auto-restart loop. Probe briefly for an early exit and, if
        # the process died, raise with the captured log tail so the API returns
        # the real reason to the caller.
        early_rc = self._await_early_exit(inst.process, timeout=2.0)
        if early_rc is not None:
            with self._lock:
                inst.user_stopped = True  # don't auto-restart a broken boot
                self._close_log(inst)
                inst.process = None
            tail = "".join(self.tail_log(user_id, lines=25)).strip()
            self._db.update_instance_status(
                user_id,
                STATUS_CRASHED,
                pid=None,
                last_error=f"exited rc={early_rc} during startup",
            )
            raise RuntimeError(
                f"trading instance exited immediately (rc={early_rc}). "
                f"Recent log:\n{tail}"
            )

        self._db.update_instance_status(
            user_id, STATUS_RUNNING, pid=inst.process.pid if inst.process else None
        )
        return self.instance_status(user_id)

    def _spawn(
        self, inst: _Instance, broker_credentials: dict[str, dict[str, Any]]
    ) -> None:
        """Build env + working dir and launch the subprocess (caller holds lock)."""
        overrides = self._load_overrides(inst.user_id)
        config_path = inst.workdir / "user_config.json"
        write_user_config(config_path, overrides)

        # Allocate a fresh loopback port for this instance's read-only dashboard
        # API. The engine serves the live-state panels (scanner, votes, ranker,
        # decisions, traces, orchestrator) there; the API control plane proxies
        # them to the authenticated owner. Bound to 127.0.0.1, so only reachable
        # from this host (the API server).
        dashboard_port = self._alloc_loopback_port()
        inst.dashboard_port = dashboard_port

        env = build_instance_environment(
            user_id=inst.user_id,
            workdir=inst.workdir,
            config_path=config_path,
            api_db_path=self._config.database_path,
            broker_credentials=broker_credentials,
            dashboard_port=dashboard_port,
            repo_root=self._repo_root,
        )
        # Force the per-instance dashboard onto loopback and disable its API-key
        # auth so the local control plane can proxy live panels without a shared
        # secret. The proxy already authenticates the owner via JWT before
        # forwarding, and the dashboard is bound to 127.0.0.1 only (never public),
        # so a second auth layer on this hop is redundant.
        #
        # Set the key to an EMPTY STRING rather than popping it: main.py calls
        # load_dotenv() at import, which would re-inject DD_DASHBOARD_API_KEY from
        # the repo-root .env if the variable were merely absent. load_dotenv uses
        # override=False, so it skips any key already present in the environment —
        # an empty value survives and keeps the dashboard in keyless (read-only)
        # mode, which is exactly what the GET-only proxy needs.
        env["DD_DASHBOARD_BIND_HOST"] = "127.0.0.1"
        env["DD_DASHBOARD_API_KEY"] = ""

        python_exe = self._config.instance_python or sys.executable or "python"
        main_script = str(self._repo_root / "main.py")

        # Open the log file in append mode; the subprocess inherits it as both
        # stdout and stderr (loguru defaults to stderr).
        log_handle = open(inst.log_path, "a", buffering=1, encoding="utf-8")
        log_handle.write(
            f"\n===== instance start {time.strftime('%Y-%m-%dT%H:%M:%S%z')} "
            f"(restart #{inst.restart_count}, dashboard :{dashboard_port}) =====\n"
        )
        log_handle.flush()

        # New session/process-group so we can signal the whole tree on stop.
        #
        # cwd MUST be the per-user working directory (NOT the repo root). The
        # engine has many state files addressed by the relative path "data/..."
        # which resolve against the cwd; pinning cwd to ``workdir`` guarantees
        # they land in this user's isolated ``workdir/data`` (== APEX_DATA_DIR),
        # never in the operator's shared, git-backed ``<repo>/data`` junction.
        # Shared read-only resources are located via APEX_REPO_DIR instead (see
        # runtime_paths.repo_root), so they remain reachable despite this cwd.
        popen_kwargs: dict[str, Any] = {
            "cwd": str(inst.workdir),
            "env": env,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        else:  # Windows: new process group so CTRL_BREAK can reach children
            popen_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )

        # ``--dashboard`` starts the engine's read-only state API alongside the
        # (unchanged) trading loop, so the control plane can proxy live panels.
        proc = subprocess.Popen(
            [python_exe, main_script, "--dashboard"], **popen_kwargs
        )
        inst.process = proc
        inst.log_handle = log_handle
        inst.started_at = time.time()
        logger.info(
            "[proc-mgr] launched instance user={} pid={} cwd={} dashboard=:{}",
            inst.user_id,
            proc.pid,
            inst.workdir,
            dashboard_port,
        )

    # ── stop / restart ──────────────────────────────────────────────────
    def stop_instance(self, user_id: int, user_initiated: bool = True) -> dict[str, Any]:
        """Gracefully stop a user's instance (SIGTERM → wait → SIGKILL)."""
        with self._lock:
            inst = self._instances.get(user_id)
            if inst is None or inst.process is None:
                self._db.update_instance_status(user_id, STATUS_STOPPED, pid=None)
                return self.instance_status(user_id)

            inst.user_stopped = user_initiated
            proc = inst.process

        if proc.poll() is None:
            self._signal_tree(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=self._config.instance_stop_grace_seconds)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "[proc-mgr] user={} did not exit in {}s — SIGKILL",
                    user_id,
                    self._config.instance_stop_grace_seconds,
                )
                self._signal_tree(proc, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.error("[proc-mgr] user={} unkillable", user_id)

        with self._lock:
            self._close_log(inst)
            inst.process = None
        self._db.update_instance_status(user_id, STATUS_STOPPED, pid=None)
        logger.info("[proc-mgr] stopped instance user={}", user_id)
        return self.instance_status(user_id)

    def restart_instance(
        self, user_id: int, broker_credentials: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """Stop (if running) then start the user's instance."""
        self.stop_instance(user_id, user_initiated=True)
        return self.start_instance(user_id, broker_credentials)

    # ── status / logs ─────────────────────────────────────────────────────
    def instance_status(self, user_id: int) -> dict[str, Any]:
        with self._lock:
            inst = self._instances.get(user_id)
            alive = bool(inst and self._is_alive(inst))
            pid = inst.process.pid if inst and inst.process else None
            restarts = inst.restart_count if inst else 0

        row = self._db.get_instance(user_id) or {}
        status = row.get("status", STATUS_STOPPED)
        if alive:
            status = STATUS_RUNNING
        elif status == STATUS_RUNNING:
            status = STATUS_STOPPED
        return {
            "user_id": user_id,
            "status": status,
            "pid": pid,
            "alive": alive,
            "restarts": max(restarts, int(row.get("restarts", 0) or 0)),
            "last_error": row.get("last_error", ""),
            "started_at": row.get("started_at"),
            "stopped_at": row.get("stopped_at"),
            "uptime_seconds": self._uptime_seconds(user_id),
        }

    def _uptime_seconds(self, user_id: int) -> float:
        """Seconds since the live process for *user_id* started (0 if not alive)."""
        with self._lock:
            inst = self._instances.get(user_id)
            if inst and self._is_alive(inst) and inst.started_at:
                return round(time.time() - inst.started_at, 1)
        return 0.0

    def instance_dashboard_port(self, user_id: int) -> Optional[int]:
        """Loopback port of a live instance's read-only dashboard API.

        Returns ``None`` when the user has no running instance (so the engine
        state panels cannot be served yet).
        """
        with self._lock:
            inst = self._instances.get(user_id)
            if inst and self._is_alive(inst):
                return inst.dashboard_port
        return None

    # ── admin / system-wide views ─────────────────────────────────────────
    def system_uptime_seconds(self) -> float:
        """Seconds since this process manager (the API server) started."""
        return round(time.time() - self._created_at, 1)

    def active_count(self) -> int:
        """Number of currently-alive trading instances."""
        with self._lock:
            return sum(1 for i in self._instances.values() if self._is_alive(i))

    def all_statuses(self) -> list[dict[str, Any]]:
        """Status of every instance known to the DB or held in memory."""
        user_ids: set[int] = set()
        for row in self._db.list_instances():
            user_ids.add(int(row["user_id"]))
        with self._lock:
            user_ids.update(self._instances.keys())
        return [self.instance_status(uid) for uid in sorted(user_ids)]

    def tail_log(self, user_id: int, lines: int = 200) -> list[str]:
        """Return the last *lines* of the user's instance log file."""
        with self._lock:
            inst = self._instances.get(user_id)
            log_path = inst.log_path if inst else self._config.user_workdir(
                user_id
            ) / "logs" / "instance.log"
        if not Path(log_path).exists():
            return []
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                return list(deque(fh, maxlen=min(lines, _LOG_TAIL_MAX)))
        except OSError as exc:
            logger.debug("[proc-mgr] tail_log failed for {}: {}", user_id, exc)
            return []

    # ── monitor loop ──────────────────────────────────────────────────────
    def _monitor_loop(self) -> None:
        while not self._monitor_stop.wait(2.0):
            try:
                self._reap_and_restart()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[proc-mgr] monitor iteration failed: {}", exc)

    def _reap_and_restart(self) -> None:
        with self._lock:
            items = list(self._instances.items())

        for user_id, inst in items:
            proc = inst.process
            if proc is None:
                continue
            rc = proc.poll()
            if rc is None:
                continue  # still alive

            # Process exited.
            with self._lock:
                self._close_log(inst)
                inst.process = None
                inst.last_exit_at = time.time()

            if inst.user_stopped:
                self._db.update_instance_status(user_id, STATUS_STOPPED, pid=None)
                continue

            # Unexpected exit — schedule an auto-restart with backoff.
            backoff = self._config.restart_backoff_seconds
            idx = min(inst.next_backoff_idx, len(backoff) - 1)
            delay = backoff[idx]
            inst.next_backoff_idx = min(inst.next_backoff_idx + 1, len(backoff) - 1)
            self._db.update_instance_status(
                user_id,
                STATUS_CRASHED,
                pid=None,
                last_error=f"exited rc={rc}; auto-restart in {delay}s",
                increment_restarts=True,
            )
            logger.warning(
                "[proc-mgr] user={} exited rc={} — auto-restart in {}s",
                user_id,
                rc,
                delay,
            )
            threading.Timer(
                delay, self._auto_restart, args=(user_id,)
            ).start()

    def _auto_restart(self, user_id: int) -> None:
        if self._monitor_stop.is_set():
            return
        with self._lock:
            inst = self._instances.get(user_id)
            if inst is None or inst.user_stopped or self._is_alive(inst):
                return
        creds = self._load_credentials(user_id)
        if not creds:
            logger.warning(
                "[proc-mgr] user={} auto-restart aborted — no credentials", user_id
            )
            self._db.update_instance_status(
                user_id, STATUS_STOPPED, pid=None, last_error="no credentials"
            )
            return
        try:
            with self._lock:
                inst = self._instances[user_id]
                inst.restart_count += 1
                self._spawn(inst, creds)
            self._db.update_instance_status(
                user_id,
                STATUS_RUNNING,
                pid=inst.process.pid if inst.process else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[proc-mgr] auto-restart failed for {}: {}", user_id, exc)
            self._db.update_instance_status(
                user_id, STATUS_CRASHED, pid=None, last_error=str(exc)
            )

    # ── reconciliation on startup ─────────────────────────────────────────
    def reconcile_on_startup(self) -> None:
        """Mark any DB instances as STOPPED — spawned processes do not survive
        an API restart (they were children of the previous API process)."""
        for row in self._db.list_instances():
            if row.get("status") in (STATUS_RUNNING, STATUS_STARTING):
                self._db.update_instance_status(
                    int(row["user_id"]),
                    STATUS_STOPPED,
                    pid=None,
                    last_error="API restarted — instance not resumed automatically",
                )

    # ── helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _alloc_loopback_port() -> int:
        """Reserve a free ephemeral TCP port on loopback and return it.

        Binds to ('127.0.0.1', 0) to let the OS pick a free port, then releases
        it so the spawned instance can claim it. A brief race window exists
        between release and re-bind, acceptable here because instances start
        infrequently and the engine simply errors+restarts on the rare clash.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _await_early_exit(
        proc: Optional[subprocess.Popen], timeout: float
    ) -> Optional[int]:
        """Wait up to *timeout*s for *proc* to exit; return its rc or None.

        Used right after spawn to catch a process that dies on startup so the
        failure can be surfaced to the caller instead of being swallowed.
        """
        if proc is None:
            return None
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    @staticmethod
    def _is_alive(inst: _Instance) -> bool:
        return inst.process is not None and inst.process.poll() is None

    @staticmethod
    def _signal_tree(proc: subprocess.Popen, sig: int) -> None:
        """Signal the process (group on POSIX) — best-effort."""
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), sig)
            else:
                proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            try:
                proc.send_signal(sig)
            except OSError:
                pass

    @staticmethod
    def _close_log(inst: _Instance) -> None:
        if inst.log_handle is not None:
            try:
                inst.log_handle.flush()
                inst.log_handle.close()
            except OSError:
                pass
            inst.log_handle = None

    def _load_overrides(self, user_id: int) -> dict[str, Any]:
        raw = self._db.get_user_config(user_id)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def _load_credentials(self, user_id: int) -> dict[str, dict[str, Any]]:
        """Decrypt all stored broker credentials for a user (for auto-restart).

        The vault is read from app-level state via :attr:`credential_loader`,
        injected by the app on startup to avoid a hard import cycle.
        """
        loader = getattr(self, "credential_loader", None)
        if loader is None:
            return {}
        try:
            return loader(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[proc-mgr] credential load failed for {}: {}", user_id, exc
            )
            return {}
