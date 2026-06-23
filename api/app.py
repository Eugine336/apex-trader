"""APEX TRADER — Multi-tenant API application factory.

Wires together the database, broker vault, process manager, auth rate limiter,
and all route modules into a single FastAPI app. The app owns the shared
singletons on ``app.state`` so the dependency functions in :mod:`api.auth`
resolve them per request.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from api.auth import RateLimiter
from api.broker_vault import BrokerVault
from api.config import ApiConfig, get_api_config
from api.credentials import load_decrypted_credentials
from api.database import Database
from api.process_manager import ProcessManager
from api.routes import admin as admin_routes
from api.routes import auth as auth_routes
from api.routes import broker as broker_routes
from api.routes import config as config_routes
from api.routes import dashboard as dashboard_routes
from api.routes import engine_proxy as engine_proxy_routes
from api.routes import trading as trading_routes
from api.routes import users as users_routes


def create_app(config: ApiConfig | None = None) -> FastAPI:
    """Build and return the configured FastAPI application."""
    config = config or get_api_config()

    db = Database(config.database_path)
    vault = BrokerVault(config.vault_master_key)
    process_manager = ProcessManager(config, db)

    # Inject a credential loader so the process manager can decrypt creds for
    # crash auto-restarts without importing the route layer (avoids a cycle).
    def _credential_loader(user_id: int) -> dict[str, dict[str, Any]]:
        return load_decrypted_credentials(db, vault, user_id)

    process_manager.credential_loader = _credential_loader  # type: ignore[attr-defined]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: reconcile orphaned instance rows + start the supervisor.
        process_manager.reconcile_on_startup()
        process_manager.start_monitor()
        process_manager.start_data_sync()
        logger.info(
            "[api] APEX multi-tenant API ready — db={} instances_dir={}",
            config.database_path,
            config.instances_dir,
        )
        yield
        # Shutdown: stop all instances + the monitor thread.
        logger.info("[api] shutting down — stopping all instances")
        process_manager.shutdown()

    app = FastAPI(
        title="APEX Trader — Multi-Tenant API",
        version="1.0.0",
        description="Control plane for per-user isolated APEX trading instances.",
        lifespan=lifespan,
    )

    app.state.config = config
    app.state.db = db
    app.state.vault = vault
    app.state.process_manager = process_manager
    app.state.rate_limiter = RateLimiter(
        config.auth_rate_limit_max_attempts,
        config.auth_rate_limit_window_seconds,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_routes.router)
    app.include_router(users_routes.router)
    app.include_router(broker_routes.router)
    app.include_router(trading_routes.router)
    app.include_router(config_routes.router)
    app.include_router(dashboard_routes.router)
    app.include_router(engine_proxy_routes.router)
    app.include_router(admin_routes.router)

    @app.get("/api/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


# Module-level ASGI app so `uvicorn api.app:app` (and `--reload`) can import it
# directly. The canonical entry point remains `python -m api.run`.
app = create_app()
