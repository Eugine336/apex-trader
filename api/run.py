"""APEX TRADER — Multi-tenant API server entry point.

Usage:
    python -m api.run                 # bind from APEX_API_HOST / APEX_API_PORT
    APEX_API_PORT=9000 python -m api.run

The server hosts the control plane (auth, broker vault, instance management,
dashboard data). Each user's trading engine runs as a separate subprocess
managed by :class:`api.process_manager.ProcessManager`.
"""

from __future__ import annotations

import uvicorn
from loguru import logger

from api.app import create_app
from api.config import get_api_config


def main() -> None:
    config = get_api_config()
    app = create_app(config)
    logger.info("Launching APEX API on http://{}:{}", config.host, config.port)
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
