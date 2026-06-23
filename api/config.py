"""APEX TRADER — API server configuration.

All API-layer settings in one place, sourced from environment variables with
safe defaults. Secret material (JWT signing key, broker-vault master key) is
generated and persisted to disk on first run if not supplied, so a fresh
deployment works out of the box while still allowing operators to inject
managed secrets via the environment.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from loguru import logger

# Root directory that holds all API + per-user runtime state.
# Defaults to ``<repo>/api_data``; override with APEX_API_DATA_DIR.
_DEFAULT_API_DATA_DIR = Path(__file__).resolve().parent.parent / "api_data"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _read_or_create_secret(path: Path, generator) -> str:
    """Return the secret stored at *path*, generating + persisting it if absent.

    The file is written with ``0600`` permissions so the secret is not
    world-readable. Used for the JWT signing key and the vault master key when
    the operator has not supplied them through the environment.
    """
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        path.parent.mkdir(parents=True, exist_ok=True)
        value = generator()
        path.write_text(value, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:  # noqa: BLE001 — best-effort on platforms without chmod
            pass
        logger.warning(
            "[api-config] generated new secret at {} — back this up; "
            "rotating it invalidates issued tokens / encrypted credentials",
            path,
        )
        return value
    except OSError as exc:
        # Last-resort ephemeral secret. Tokens/creds won't survive a restart,
        # but the server still boots instead of crashing.
        logger.error("[api-config] could not persist secret at {}: {}", path, exc)
        return generator()


@dataclass
class ApiConfig:
    """Resolved API configuration. Build via :func:`get_api_config`."""

    api_data_dir: Path = field(
        default_factory=lambda: Path(
            _env("APEX_API_DATA_DIR") or str(_DEFAULT_API_DATA_DIR)
        )
    )

    # ── Auth (JWT) ────────────────────────────────────────────────────────
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = field(
        default_factory=lambda: _env_int("APEX_API_ACCESS_TTL_MIN", 30)
    )
    refresh_token_ttl_days: int = field(
        default_factory=lambda: _env_int("APEX_API_REFRESH_TTL_DAYS", 7)
    )
    password_reset_ttl_minutes: int = field(
        default_factory=lambda: _env_int("APEX_API_RESET_TTL_MIN", 30)
    )

    # ── CORS ──────────────────────────────────────────────────────────────
    cors_origins: list[str] = field(
        default_factory=lambda: [
            o.strip()
            for o in _env(
                "APEX_API_CORS_ORIGINS",
                "http://localhost:3000,http://127.0.0.1:3000,"
                "https://apex-trader.live,https://www.apex-trader.live",
            ).split(",")
            if o.strip()
        ]
    )

    # ── Rate limiting (auth endpoints) ────────────────────────────────────
    auth_rate_limit_max_attempts: int = field(
        default_factory=lambda: _env_int("APEX_API_AUTH_RATE_MAX", 10)
    )
    auth_rate_limit_window_seconds: int = field(
        default_factory=lambda: _env_int("APEX_API_AUTH_RATE_WINDOW", 60)
    )

    # ── Process manager ───────────────────────────────────────────────────
    max_instances: int = field(
        default_factory=lambda: _env_int("APEX_API_MAX_INSTANCES", 50)
    )
    instance_stop_grace_seconds: int = field(
        default_factory=lambda: _env_int("APEX_API_STOP_GRACE_SEC", 15)
    )
    instance_python: str = field(
        default_factory=lambda: _env("APEX_API_PYTHON")
    )
    # Auto-restart backoff schedule (seconds) for crashed instances.
    restart_backoff_seconds: tuple[int, ...] = (5, 15, 60, 300)

    # ── Server bind ───────────────────────────────────────────────────────
    host: str = field(default_factory=lambda: _env("APEX_API_HOST") or "127.0.0.1")
    port: int = field(default_factory=lambda: _env_int("APEX_API_PORT", 8080))

    # Secrets are resolved lazily after api_data_dir is known.
    jwt_secret: str = ""
    vault_master_key: str = ""

    def __post_init__(self) -> None:
        self.api_data_dir = Path(self.api_data_dir)
        self.api_data_dir.mkdir(parents=True, exist_ok=True)

        self.jwt_secret = _env("APEX_API_JWT_SECRET") or _read_or_create_secret(
            self.api_data_dir / ".jwt_secret",
            lambda: secrets.token_urlsafe(64),
        )
        self.vault_master_key = _env("APEX_API_VAULT_KEY") or _read_or_create_secret(
            self.api_data_dir / ".vault_master_key",
            # urlsafe 32-byte key — suitable raw material for HKDF/Fernet derivation.
            lambda: secrets.token_urlsafe(32),
        )

    # ── Derived paths ─────────────────────────────────────────────────────
    @property
    def database_path(self) -> Path:
        return self.api_data_dir / "apex_api.db"

    @property
    def instances_dir(self) -> Path:
        """Root for per-user instance working directories."""
        d = self.api_data_dir / "instances"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def user_workdir(self, user_id: int) -> Path:
        """Per-user working directory (holds data/, logs/, config, pid file)."""
        return self.instances_dir / f"user_{user_id}"


@lru_cache(maxsize=1)
def get_api_config() -> ApiConfig:
    """Return the process-wide singleton :class:`ApiConfig`."""
    return ApiConfig()


def reset_api_config_cache() -> None:
    """Clear the cached config (used by tests that override env vars)."""
    get_api_config.cache_clear()
