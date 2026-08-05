"""APEX TRADER — Action adapters (Composio + mock).

An adapter is the *only* thing that actually touches an external system. The
:class:`~action.orchestrator.ActionOrchestrator` calls ``adapter.execute(
capability, params)`` and expects an :class:`~action.orchestrator.ActionResult`.
Everything above the adapter (reasoning, governance, audit, memory) is
provider-agnostic; swapping Composio for anything else is a matter of swapping
the adapter, not touching the cognitive loop.

* :class:`MockActionAdapter` — performs no external I/O. It records the calls
  and returns success, so the entire action layer is exercisable offline and is
  the adapter used whenever the layer is disabled or in ``dry_run`` mode.
* :class:`ComposioAdapter` — executes a capability through Composio's REST API
  using only the standard library, behind an injectable ``transport`` so the
  request-shaping / response-parsing is testable with no network. Fail-safe and
  secret-safe: any error becomes a failed :class:`ActionResult`, and the API key
  is never logged.

NOTE: the adapter targets Composio's current **v3** REST API by default
(``POST /api/v3/tools/execute/{tool_slug}``); the retired v2 shape is kept as an
explicit fallback via ``api_version="v2"`` (env ``COMPOSIO_API_VERSION``). The
network call is isolated to the ``transport`` and the request builder, so
aligning the shape later is a one-place change with the tests still valid.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from loguru import logger

from action.orchestrator import ActionResult

Transport = Callable[[str, dict, bytes, float], "tuple[int, str]"]


def _urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> "tuple[int, str]":
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200) or 200), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        return int(exc.code or 0), detail
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def _mock_knowledge_payload(capability: str, params: dict) -> dict:
    """Representative READ payload for the offline/dry-run adapter.

    Lets the whole knowledge/research/advisor → Evidence path be exercised with
    no network: knowledge/research capabilities return a couple of search-style
    items; an advisor capability returns one advisory answer. Non-read
    capabilities (notify/ticket/docs) return an empty payload, as they do live.
    """
    cap = str(capability or "").lower()
    query = str((params or {}).get("query", "") or "")
    if "advis" in cap or "consult" in cap or "reason" in cap:
        return {
            "answer": f"Advisory read on {query or 'the market'}: mixed context, no strong edge.",
            "direction": "NEUTRAL",
            "confidence": 0.4,
            "advisor": "mock-advisor",
        }
    if "knowledge" in cap or "research" in cap or "search" in cap or "retriev" in cap:
        return {
            "items": [
                {"title": f"Context for {query or 'market'}",
                 "snippet": "Mock research passage — offline placeholder.",
                 "source": "mock", "sentiment": "neutral"},
            ],
        }
    return {}


class MockActionAdapter:
    """No-op adapter — records calls, performs no external I/O, always succeeds."""

    name = "mock"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._seq = 0

    def execute(self, capability: str, params: dict) -> ActionResult:
        self._seq += 1
        self.calls.append({"capability": capability, "params": dict(params or {})})
        return ActionResult(
            ok=True,
            external_ref=f"mock:{capability}#{self._seq}",
            detail="mock/dry-run — no external call made",
            verified=True,
            data=_mock_knowledge_payload(capability, params),
        )


class ComposioAdapter:
    """Executes a capability via Composio's REST API (stdlib only). Fail-safe."""

    name = "composio"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://backend.composio.dev",
        entity_id: str = "default",
        timeout_seconds: float = 20.0,
        api_version: str = "v3",
        transport: Optional[Transport] = None,
    ) -> None:
        self._api_key = str(api_key or "")
        self._base_url = str(base_url or "https://backend.composio.dev").rstrip("/")
        self._entity_id = str(entity_id or "default")
        self._timeout = float(timeout_seconds or 20.0)
        self._api_version = str(api_version or "v3").strip().lower().lstrip("v") or "3"
        self._transport: Transport = transport or _urllib_transport

    @property
    def usable(self) -> bool:
        return bool(self._api_key)

    def _build_request(self, capability: str, params: dict) -> "tuple[str, dict, dict]":
        """Return ``(url, headers, payload)`` for the configured API version.

        v3 (current) executes a tool by slug: ``POST /api/v3/tools/execute/{slug}``
        with ``{"user_id", "arguments"}``. The retired v2 shape
        (``/api/v2/actions/{action}/execute`` with ``{"entityId", "input"}``) is
        kept only as an explicit fallback for older deployments.
        """
        headers = {"content-type": "application/json", "x-api-key": self._api_key}
        args = dict(params or {})
        if self._api_version == "2":
            url = f"{self._base_url}/api/v2/actions/{capability}/execute"
            return url, headers, {"entityId": self._entity_id, "input": args}
        # Default: v3.
        url = f"{self._base_url}/api/v3/tools/execute/{capability}"
        return url, headers, {"user_id": self._entity_id, "arguments": args}

    def execute(self, capability: str, params: dict) -> ActionResult:
        url, headers, payload = self._build_request(capability, params)
        try:
            body = json.dumps(payload).encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            return ActionResult(ok=False, detail=f"payload encode failed: {exc}")
        try:
            status, text = self._transport(url, headers, body, self._timeout)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[composio] transport error ({}): {}", capability, exc)
            return ActionResult(ok=False, detail=f"transport error: {type(exc).__name__}")
        if status < 200 or status >= 300:
            logger.warning("[composio] {} HTTP {} — {}", capability, status, (text or "")[:160])
            return ActionResult(ok=False, detail=f"HTTP {status}")
        return self._parse(capability, text)

    @staticmethod
    def _parse(capability: str, text: str) -> ActionResult:
        try:
            data = json.loads(text) if text else {}
        except Exception as exc:  # noqa: BLE001
            return ActionResult(ok=False, detail=f"bad JSON: {exc}")
        if not isinstance(data, dict):
            return ActionResult(ok=True, external_ref="", detail="ok")
        # Composio v3 wraps the tool result under "data" with a "successful" flag
        # and an "error" string on failure; be liberal about older shapes too.
        err = data.get("error")
        ok = bool(data.get("successful", data.get("success", True))) and not err
        ref = ""
        if data.get("id") is not None:
            ref = str(data.get("id"))
        elif isinstance(data.get("data"), dict) and data["data"].get("id") is not None:
            ref = str(data["data"]["id"])
        # Preserve the tool's returned payload so READ capabilities can become
        # Evidence. Composio nests the tool output under "data" (v3) or
        # "response_data"; fall back to the whole object when it doesn't.
        payload: dict = {}
        for key in ("data", "response_data", "result", "output"):
            val = data.get(key)
            if isinstance(val, dict) and val:
                payload = val
                break
        if not payload and any(
            k in data for k in ("items", "answer", "results", "content")
        ):
            payload = data
        detail = "composio ok" if ok else (
            f"composio error: {str(err)[:120]}" if err else "composio reported failure"
        )
        return ActionResult(ok=ok, external_ref=ref, detail=detail, data=payload)


def build_adapter(config: Any, transport: Optional[Transport] = None) -> Any:
    """Pick an adapter from config.

    Returns a :class:`MockActionAdapter` whenever the layer is disabled, in
    ``dry_run`` mode, or missing an API key — so real external calls only happen
    on an explicit, fully-configured opt-in. Otherwise a :class:`ComposioAdapter`.
    """
    enabled = bool(getattr(config, "enabled", False)) if config is not None else False
    dry_run = bool(getattr(config, "dry_run", True)) if config is not None else True
    api_key = str(getattr(config, "api_key", "") or "") if config is not None else ""
    if not enabled or dry_run or not api_key:
        return MockActionAdapter()
    transport_mode = str(
        getattr(config, "transport", "rest") or "rest"
    ).strip().lower() if config is not None else "rest"
    if transport_mode == "mcp":
        from action.mcp_client import McpActionAdapter  # lazy — keeps import light
        return McpActionAdapter(
            api_key,
            url=str(getattr(config, "mcp_url", "") or "https://connect.composio.dev/mcp"),
            auth_header=str(getattr(config, "mcp_auth_header", "") or "x-consumer-api-key"),
            entity_id=str(getattr(config, "entity_id", "default") or "default"),
            timeout_seconds=float(getattr(config, "timeout_seconds", 30.0) or 30.0),
            router_tool=str(
                getattr(config, "mcp_router_tool", "") or "COMPOSIO_MULTI_EXECUTE_TOOL"
            ),
            router_tools_key=str(
                getattr(config, "mcp_router_tools_key", "") or "tool_calls"
            ),
            router_slug_key=str(
                getattr(config, "mcp_router_slug_key", "") or "tool_slug"
            ),
            router_args_key=str(
                getattr(config, "mcp_router_args_key", "") or "arguments"
            ),
        )
    return ComposioAdapter(
        api_key,
        base_url=str(getattr(config, "base_url", "") or "https://backend.composio.dev"),
        entity_id=str(getattr(config, "entity_id", "default") or "default"),
        timeout_seconds=float(getattr(config, "timeout_seconds", 20.0) or 20.0),
        api_version=str(getattr(config, "api_version", "v3") or "v3"),
        transport=transport,
    )


__all__ = ["MockActionAdapter", "ComposioAdapter", "build_adapter", "Transport"]
