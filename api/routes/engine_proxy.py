"""APEX TRADER — Engine state proxy routes.

Live read-only engine panels (scanner, module votes, ranker, decisions,
decision traces, orchestrator) are served by each user's isolated trading
subprocess on a loopback-only dashboard port (see
:meth:`api.process_manager.ProcessManager._spawn`). These routes forward the
authenticated owner's request to *their* instance and return the response
verbatim, so the multi-tenant frontend reuses the exact data shapes the
original single-user dashboard produced.

Every route is JWT-gated via :func:`api.auth.get_current_user`; a user can only
ever reach their own instance because the target port is resolved from the
process manager keyed by ``user["id"]``. When the user's instance is not
running (or its dashboard has not finished binding yet) the routes return 503
so the frontend can prompt the user to start trading.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger

from api.auth import get_current_user, get_process_manager
from api.process_manager import ProcessManager

router = APIRouter(prefix="/api/engine", tags=["engine"])

# Short timeout: the loopback dashboard is local, so a slow response means the
# engine is busy or the port is not yet listening — fail fast to 503.
_PROXY_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


async def _proxy(
    *,
    user: dict[str, Any],
    pm: ProcessManager,
    path: str,
    params: Optional[dict[str, Any]] = None,
) -> Any:
    """Forward a GET to the user's instance dashboard and return its JSON.

    Raises 503 when the instance is not running or unreachable.
    """
    port = pm.instance_dashboard_port(int(user["id"]))
    if not port:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="trading instance not running — start it to view live engine data",
        )

    url = f"http://127.0.0.1:{port}{path}"
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    try:
        async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT) as http:
            resp = await http.get(url, params=clean)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        # The engine answered but with an error status — surface it as a 502.
        logger.debug(
            "[engine-proxy] user={} {} → upstream {}",
            user.get("id"),
            path,
            exc.response.status_code,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"engine returned {exc.response.status_code}",
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        # Connection refused / timeout / bad JSON — instance likely still
        # starting up or mid-restart.
        logger.debug("[engine-proxy] user={} {} unreachable: {}", user.get("id"), path, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="engine data not available yet — the instance may still be starting",
        ) from exc


@router.get("/scanner")
async def scanner(
    user: dict[str, Any] = Depends(get_current_user),
    pm: ProcessManager = Depends(get_process_manager),
) -> Any:
    """Per-instrument scanner board — score, direction, status, factor grid."""
    return await _proxy(user=user, pm=pm, path="/api/scanner")


@router.get("/module-votes")
async def module_votes(
    user: dict[str, Any] = Depends(get_current_user),
    pm: ProcessManager = Depends(get_process_manager),
) -> Any:
    """Per-pair grid of brain-module directional votes + per-module stats."""
    return await _proxy(user=user, pm=pm, path="/api/module-votes")


@router.get("/ranker")
async def ranker(
    user: dict[str, Any] = Depends(get_current_user),
    pm: ProcessManager = Depends(get_process_manager),
) -> Any:
    """Ranked opportunities (coherent vote clusters) per pair with EV/horizon."""
    return await _proxy(user=user, pm=pm, path="/api/ranker")


@router.get("/decisions")
async def decisions(
    limit: int = Query(default=100, ge=1, le=500),
    decision_type: str = Query(default=""),
    symbol: str = Query(default=""),
    user: dict[str, Any] = Depends(get_current_user),
    pm: ProcessManager = Depends(get_process_manager),
) -> Any:
    """Recent decisions from the Decision Intelligence layer + aggregate stats."""
    return await _proxy(
        user=user,
        pm=pm,
        path="/api/decisions",
        params={"limit": limit, "decision_type": decision_type, "symbol": symbol},
    )


@router.get("/decision-trace")
async def decision_trace(
    limit: int = Query(default=150, ge=1, le=500),
    symbol: str = Query(default=""),
    user: dict[str, Any] = Depends(get_current_user),
    pm: ProcessManager = Depends(get_process_manager),
) -> Any:
    """Pipeline awareness traces — stage chains, justifications, challenges."""
    return await _proxy(
        user=user,
        pm=pm,
        path="/api/decision-trace",
        params={"limit": limit, "symbol": symbol},
    )


@router.get("/orchestrator")
async def orchestrator(
    limit: int = Query(default=100, ge=1, le=500),
    symbol: str = Query(default=""),
    user: dict[str, Any] = Depends(get_current_user),
    pm: ProcessManager = Depends(get_process_manager),
) -> Any:
    """Round-table graded-sizing proposals — per-dimension multipliers + size."""
    return await _proxy(
        user=user,
        pm=pm,
        path="/api/orchestrator",
        params={"limit": limit, "symbol": symbol},
    )


@router.get("/risk")
async def risk(
    user: dict[str, Any] = Depends(get_current_user),
    pm: ProcessManager = Depends(get_process_manager),
) -> Any:
    """Compliance risk monitor — mode, drawdown, exposure, execution quality."""
    return await _proxy(user=user, pm=pm, path="/api/risk")


@router.get("/governor")
async def governor(
    user: dict[str, Any] = Depends(get_current_user),
    pm: ProcessManager = Depends(get_process_manager),
) -> Any:
    """Portfolio governor — daily loss cap, exposure caps, recent blocks."""
    return await _proxy(user=user, pm=pm, path="/api/governor")


@router.get("/planner")
async def planner(
    limit: int = Query(default=100, ge=1, le=500),
    symbol: str = Query(default=""),
    user: dict[str, Any] = Depends(get_current_user),
    pm: ProcessManager = Depends(get_process_manager),
) -> Any:
    """Trade planner journal — recent plans, outcomes, strategy distributions."""
    return await _proxy(
        user=user,
        pm=pm,
        path="/api/planner",
        params={"limit": limit, "symbol": symbol},
    )


@router.get("/operations")
async def operations(
    user: dict[str, Any] = Depends(get_current_user),
    pm: ProcessManager = Depends(get_process_manager),
) -> Any:
    """Ops control room — health, drawdown, exposure, regime, tick latency."""
    return await _proxy(user=user, pm=pm, path="/api/operations")


@router.get("/active-trades")
async def active_trades(
    user: dict[str, Any] = Depends(get_current_user),
    pm: ProcessManager = Depends(get_process_manager),
) -> Any:
    """Currently open positions with live P&L, SL/TP, lots, score and stage."""
    return await _proxy(user=user, pm=pm, path="/api/trades")


@router.get("/position-health")
async def position_health(
    limit: int = Query(default=200, ge=1, le=1000),
    symbol: str = Query(default=""),
    user: dict[str, Any] = Depends(get_current_user),
    pm: ProcessManager = Depends(get_process_manager),
) -> Any:
    """Per-position health scores, dimension breakdowns and management actions."""
    return await _proxy(
        user=user,
        pm=pm,
        path="/api/position-health",
        params={"limit": limit, "symbol": symbol},
    )


@router.get("/history")
async def history(
    user: dict[str, Any] = Depends(get_current_user),
    pm: ProcessManager = Depends(get_process_manager),
) -> Any:
    """Closed-trade history rows for the engine's own trade journal."""
    return await _proxy(user=user, pm=pm, path="/api/history")


@router.get("/performance")
async def performance(
    user: dict[str, Any] = Depends(get_current_user),
    pm: ProcessManager = Depends(get_process_manager),
) -> Any:
    """Aggregate performance — win rate, profit factor, equity curve, extremes."""
    return await _proxy(user=user, pm=pm, path="/api/performance")
