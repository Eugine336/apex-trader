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

NOTE: the exact Composio request/response shape must be confirmed against the
live API + your key (it could not be reached from the build sandbox). The
network call is isolated to the ``transport`` and the request builder, so
aligning it later is a one-place change with the tests still valid.
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
        transport: Optional[Transport] = None,
    ) -> None:
        self._api_key = str(api_key or "")
        self._base_url = str(base_url or "https://backend.composio.dev").rstrip("/")
        self._entity_id = str(entity_id or "default")
        self._timeout = float(timeout_seconds or 20.0)
        self._transport: Transport = transport or _urllib_transport

    @property
    def usable(self) -> bool:
        return bool(self._api_key)

    def execute(self, capability: str, params: dict) -> ActionResult:
        # Composio maps a capability name to an "action" and executes it for an
        # entity. Shape must be verified against the live API (see module note).
        url = f"{self._base_url}/api/v2/actions/{capability}/execute"
        headers = {"content-type": "application/json", "x-api-key": self._api_key}
        payload = {"entityId": self._entity_id, "input": dict(params or {})}
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
        # Composio commonly wraps the tool result and a success flag; be liberal.
        ok = bool(data.get("successful", data.get("success", True)))
        ref = ""
        if data.get("id") is not None:
            ref = str(data.get("id"))
        elif isinstance(data.get("data"), dict) and data["data"].get("id") is not None:
            ref = str(data["data"]["id"])
        return ActionResult(ok=ok, external_ref=ref, detail="composio ok" if ok else "composio reported failure")


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
    return ComposioAdapter(
        api_key,
        base_url=str(getattr(config, "base_url", "") or "https://backend.composio.dev"),
        entity_id=str(getattr(config, "entity_id", "default") or "default"),
        timeout_seconds=float(getattr(config, "timeout_seconds", 20.0) or 20.0),
        transport=transport,
    )


__all__ = ["MockActionAdapter", "ComposioAdapter", "build_adapter", "Transport"]
