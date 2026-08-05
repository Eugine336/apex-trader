"""APEX TRADER — Composio MCP client adapter (Model Context Protocol).

Composio exposes a hosted **MCP** server (e.g. ``https://connect.composio.dev/mcp``)
alongside its REST API. MCP is JSON-RPC 2.0 over the "Streamable HTTP" transport:
the client POSTs JSON-RPC messages and the server replies with either
``application/json`` or a ``text/event-stream`` (SSE) body. A session is
established with an ``initialize`` handshake (the server returns an
``Mcp-Session-Id`` header that subsequent requests echo back), then tools are
invoked with ``tools/call``.

:class:`McpActionAdapter` speaks that protocol but exposes the SAME
``execute(capability, params) -> ActionResult`` interface as
:class:`~action.composio.ComposioAdapter`, so it is a drop-in for the
:class:`~action.orchestrator.ActionOrchestrator` and the cognition
``KnowledgeSource`` — nothing above the adapter changes.

Stdlib-only, secret-safe (the key is never logged), fail-safe (any error becomes
a failed :class:`ActionResult`), and the network call is isolated behind an
injectable ``transport`` so the request-shaping / SSE-parsing is fully testable
with no network.

Authentication: an API key in the ``x-consumer-api-key`` header (Composio's MCP
convention). OAuth-authorised clients are out of scope for this headless adapter.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from loguru import logger

from action.orchestrator import ActionResult

# (status, response_headers_lowercased, body_text)
McpTransport = Callable[[str, dict, bytes, float], "tuple[int, dict, str]"]


def _urllib_mcp_transport(url: str, headers: dict, body: bytes, timeout: float) -> "tuple[int, dict, str]":
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            hdrs = {str(k).lower(): str(v) for k, v in resp.headers.items()}
            text = resp.read().decode("utf-8", "replace")
            return status, hdrs, text
    except urllib.error.HTTPError as exc:
        detail = ""
        hdrs = {}
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:  # noqa: BLE001
            pass
        try:
            hdrs = {str(k).lower(): str(v) for k, v in exc.headers.items()}
        except Exception:  # noqa: BLE001
            pass
        return int(exc.code or 0), hdrs, detail
    except Exception as exc:  # noqa: BLE001
        return 0, {}, f"{type(exc).__name__}: {exc}"


def _extract_jsonrpc_messages(text: str, content_type: str) -> "list[dict]":
    """Parse a JSON or SSE response body into JSON-RPC message dicts. Fail-safe."""
    out: list[dict] = []
    if not text:
        return out
    ct = (content_type or "").lower()
    looks_sse = (
        "text/event-stream" in ct
        or text.lstrip().startswith("event:")
        or text.lstrip().startswith("data:")
        or "\ndata:" in text
    )
    if looks_sse:
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(obj, dict):
                out.append(obj)
        if out:
            return out
    # Plain JSON (single object or a batch array).
    try:
        obj = json.loads(text)
    except Exception:  # noqa: BLE001
        return out
    if isinstance(obj, list):
        out.extend([o for o in obj if isinstance(o, dict)])
    elif isinstance(obj, dict):
        out.append(obj)
    return out


class McpActionAdapter:
    """Executes a capability via a Composio MCP server (JSON-RPC 2.0). Fail-safe."""

    name = "composio-mcp"

    def __init__(
        self,
        api_key: str,
        *,
        url: str = "https://connect.composio.dev/mcp",
        auth_header: str = "x-consumer-api-key",
        entity_id: str = "default",
        timeout_seconds: float = 30.0,
        protocol_version: str = "2025-06-18",
        transport: Optional[McpTransport] = None,
    ) -> None:
        self._api_key = str(api_key or "")
        self._url = str(url or "https://connect.composio.dev/mcp")
        self._auth_header = str(auth_header or "x-consumer-api-key").lower()
        self._entity_id = str(entity_id or "default")
        self._timeout = float(timeout_seconds or 30.0)
        self._protocol_version = str(protocol_version or "2025-06-18")
        self._transport: McpTransport = transport or _urllib_mcp_transport
        self._session_id = ""
        self._initialized = False
        self._id = 0

    @property
    def usable(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict:
        h = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            self._auth_header: self._api_key,
            "mcp-protocol-version": self._protocol_version,
        }
        if self._session_id:
            h["mcp-session-id"] = self._session_id
        return h

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _post(self, payload: dict) -> "tuple[int, dict, list]":
        try:
            body = json.dumps(payload).encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.debug("[mcp] payload encode failed: {}", exc)
            return 0, {}, []
        status, hdrs, text = self._transport(self._url, self._headers(), body, self._timeout)
        msgs = _extract_jsonrpc_messages(text, hdrs.get("content-type", ""))
        return status, hdrs, msgs

    def _ensure_session(self) -> bool:
        """Perform the MCP initialize handshake once; cache the session id."""
        if self._initialized:
            return True
        init = {
            "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
            "params": {
                "protocolVersion": self._protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "apex-trader", "version": "1.0"},
            },
        }
        status, hdrs, _msgs = self._post(init)
        if status < 200 or status >= 300:
            logger.warning("[mcp] initialize HTTP {}", status)
            return False
        sid = hdrs.get("mcp-session-id", "") or hdrs.get("mcp-session_id", "")
        if sid:
            self._session_id = sid
        # Notify the server the client is ready (a notification — no response).
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception as exc:  # noqa: BLE001
            logger.debug("[mcp] initialized notification ignored a fault: {}", exc)
        self._initialized = True
        return True

    def execute(self, capability: str, params: dict) -> ActionResult:
        if not self._api_key:
            return ActionResult(ok=False, detail="mcp: no api key")
        try:
            if not self._ensure_session():
                return ActionResult(ok=False, detail="mcp: initialize failed")
            call = {
                "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
                "params": {"name": capability, "arguments": dict(params or {})},
            }
            status, _hdrs, msgs = self._post(call)
            if status < 200 or status >= 300:
                logger.warning("[mcp] {} HTTP {}", capability, status)
                # A rejected/expired session — reset so the next call re-handshakes.
                if status in (400, 401, 403, 404):
                    self._initialized = False
                    self._session_id = ""
                return ActionResult(ok=False, detail=f"HTTP {status}")
            return self._parse(capability, msgs)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[mcp] transport error ({}): {}", capability, exc)
            return ActionResult(ok=False, detail=f"transport error: {type(exc).__name__}")

    @staticmethod
    def _parse(capability: str, msgs: list) -> ActionResult:
        resp = None
        for m in msgs:
            if isinstance(m, dict) and ("result" in m or "error" in m):
                resp = m  # last response wins (typically exactly one)
        if resp is None:
            return ActionResult(ok=False, detail="mcp: no JSON-RPC response")
        if resp.get("error"):
            err = resp["error"]
            msg = err.get("message") if isinstance(err, dict) else err
            return ActionResult(ok=False, detail=f"mcp error: {str(msg)[:160]}")
        result = resp.get("result") if isinstance(resp.get("result"), dict) else {}
        is_error = bool(result.get("isError", False))
        # Preferred payload is the tool's structuredContent; else the first text
        # content block (parsed as JSON when it is one, else wrapped as an answer)
        # so a research/advisor text reply still becomes usable Evidence.
        payload: dict = {}
        sc = result.get("structuredContent")
        if isinstance(sc, dict) and sc:
            payload = sc
        else:
            text = ""
            content = result.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                        text = str(block["text"])
                        break
            if text:
                try:
                    parsed = json.loads(text)
                    payload = parsed if isinstance(parsed, dict) else {"answer": text}
                except Exception:  # noqa: BLE001
                    payload = {"answer": text}
        ok = not is_error
        return ActionResult(
            ok=ok, external_ref="", data=payload,
            detail="mcp ok" if ok else "mcp tool reported an error",
        )


__all__ = ["McpActionAdapter", "McpTransport"]
