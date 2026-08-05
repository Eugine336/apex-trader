#!/usr/bin/env python3
"""Standalone Composio MCP connectivity test — bypasses COMPOSIO_DRY_RUN.

Run this from a machine with real network access to connect.composio.dev
(the sandbox this was written in cannot reach that host).

What it checks, in order:
  1. .env loads and COMPOSIO_API_KEY is present.
  2. MCP `initialize` handshake succeeds (network + auth header format ok).
  3. `tools/list` — confirms the key is actually authorised and shows which
     tools/apps are connected on the Composio side (empty list often means the
     key is valid but no apps/connections are linked to that entity yet).
  4. Optional: `tools/call` against a real capability, if you pass one.

Usage:
    python scripts/test_composio_mcp.py
    python scripts/test_composio_mcp.py --capability SLACK_SEND_MESSAGE --args '{"channel":"#test","text":"hi"}'

Exits non-zero on any failed step so it's CI-friendly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Make the repo importable when run as `python scripts/test_composio_mcp.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader — avoids adding a python-dotenv dependency just for this script."""
    if not path.exists():
        print(f"[warn] no .env found at {path}")
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def main() -> int:
    parser = argparse.ArgumentParser(description="Test live Composio MCP connectivity")
    parser.add_argument("--capability", default="", help="Tool slug to call, e.g. SLACK_SEND_MESSAGE")
    parser.add_argument("--args", default="{}", help="JSON arguments for --capability")
    parser.add_argument("--args-file", default="", help="Path to a JSON file with the "
                        "arguments for --capability (bulletproof on any shell — "
                        "avoids PowerShell quote-stripping)")
    parser.add_argument("--env-file", default=str(Path(__file__).resolve().parent.parent / ".env"))
    ns = parser.parse_args()

    _load_dotenv(Path(ns.env_file))

    api_key = os.getenv("COMPOSIO_API_KEY", "").strip()
    mcp_url = os.getenv("COMPOSIO_MCP_URL", "https://connect.composio.dev/mcp").strip()
    auth_header = os.getenv("COMPOSIO_MCP_AUTH_HEADER", "x-consumer-api-key").strip()
    entity_id = os.getenv("COMPOSIO_ENTITY_ID", "default").strip()

    print("=== Config ===")
    print(f"mcp_url      = {mcp_url}")
    print(f"auth_header  = {auth_header}")
    print(f"entity_id    = {entity_id}")
    print(f"api_key      = {'set (' + str(len(api_key)) + ' chars, prefix ' + api_key[:6] + ')' if api_key else 'MISSING'}")
    print(f"dry_run env  = {os.getenv('COMPOSIO_DRY_RUN', '<unset>')} (irrelevant here — this script always goes live)")
    print()

    if not api_key:
        print("[FAIL] COMPOSIO_API_KEY is empty — nothing to test. Check .env.")
        return 1

    from action.mcp_client import McpActionAdapter  # noqa: E402  (import after path fix)

    adapter = McpActionAdapter(
        api_key,
        url=mcp_url,
        auth_header=auth_header,
        entity_id=entity_id,
        timeout_seconds=20.0,
    )

    # --- Step 1: initialize handshake ---------------------------------
    print("=== Step 1: MCP initialize ===")
    ok = adapter._ensure_session()
    if not ok:
        print("[FAIL] initialize handshake failed.")
        print("  Likely causes: wrong URL, network blocked, or the server rejected")
        print("  the request before even checking auth. Check firewall/DNS next.")
        return 1
    print(f"[OK] session established. session_id={adapter._session_id or '(none returned — some servers omit it)'}")
    print()

    # --- Step 2: tools/list (proves the API key itself is accepted) ---
    print("=== Step 2: tools/list (auth + visibility check) ===")
    status, _hdrs, msgs = adapter._post({
        "jsonrpc": "2.0",
        "id": adapter._next_id(),
        "method": "tools/list",
        "params": {},
    })
    if status < 200 or status >= 300:
        print(f"[FAIL] tools/list HTTP {status}.")
        if status in (401, 403):
            print("  -> This means the handshake succeeded but the API KEY was")
            print("     rejected. Re-check COMPOSIO_API_KEY value and that it hasn't")
            print("     been rotated/revoked on the Composio dashboard.")
        return 1
    resp = next((m for m in msgs if "result" in m or "error" in m), None)
    if resp is None:
        print("[FAIL] no JSON-RPC response body for tools/list.")
        return 1
    if resp.get("error"):
        print(f"[FAIL] tools/list returned an error: {resp['error']}")
        return 1
    tools = (resp.get("result") or {}).get("tools", [])
    print(f"[OK] key accepted. {len(tools)} tool(s) visible to this entity.")
    if tools:
        for t in tools[:15]:
            print(f"    - {t.get('name')}")
        if len(tools) > 15:
            print(f"    ... and {len(tools) - 15} more")
    else:
        print("  [note] Zero tools usually means: key is valid, but no apps are")
        print("  connected/authorised for this entity_id yet on the Composio side.")
    print()

    # --- Step 2b: executor meta-tool schema --------------------------------
    # Composio's MCP server exposes generic meta-tools; app-actions are run by
    # dispatching through the executor meta-tool with the target slug +
    # arguments. Print that tool's inputSchema so the exact wrapper keys can be
    # confirmed (and, if they differ from the defaults, set via
    # COMPOSIO_MCP_ROUTER_TOOLS_KEY / _SLUG_KEY / _ARGS_KEY in .env).
    router_tool = os.getenv("COMPOSIO_MCP_ROUTER_TOOL", "COMPOSIO_MULTI_EXECUTE_TOOL").strip()
    names = [str(t.get("name")) for t in tools]
    print(f"=== Step 2b: executor meta-tool schema ({router_tool}) ===")
    if router_tool in names:
        rt = next(t for t in tools if str(t.get("name")) == router_tool)
        schema = rt.get("inputSchema") or rt.get("input_schema") or {}
        print(f"[OK] {router_tool} is advertised. App-actions will be routed through it.")
        print("  inputSchema:")
        print("    " + json.dumps(schema, indent=2)[:1500].replace("\n", "\n    "))
        print("  -> If the top-level property is not 'tool_calls', or items don't use")
        print("     'tool_slug'/'arguments', set COMPOSIO_MCP_ROUTER_TOOLS_KEY /")
        print("     COMPOSIO_MCP_ROUTER_SLUG_KEY / COMPOSIO_MCP_ROUTER_ARGS_KEY in .env.")
    else:
        print(f"  [note] {router_tool} is not advertised — the adapter will call tools by")
        print("  name directly. Adjust COMPOSIO_MCP_ROUTER_TOOL if the executor differs.")
    print()

    # --- Step 3 (optional): a real tool call ---------------------------
    if ns.capability:
        print(f"=== Step 3: tools/call ({ns.capability}) ===")
        args, err = _load_args(ns.args, ns.args_file)
        if err:
            print(f"[FAIL] {err}")
            print("  Hint (Windows PowerShell strips embedded double quotes when")
            print("  calling native programs). Use ONE of these instead:")
            print("    • a file:  --args-file args.json   (most reliable)")
            print("    • escape:  --args '{\\\"tickers\\\":\\\"FOREX:EUR\\\"}'")
            print("    • pwsh 7:  --args '{\"tickers\":\"FOREX:EUR\"}'")
            return 1
        result = adapter.execute(ns.capability, args)
        print(f"ok={result.ok} detail={result.detail!r}")
        print(f"data={json.dumps(result.data, indent=2)[:2000]}")
        return 0 if result.ok else 1

    print("(pass --capability NAME --args '{...}' to also test a live tool call)")
    return 0


def _load_args(raw: str, args_file: str) -> "tuple[dict, str]":
    """Parse tool arguments leniently. Returns (args, error_message).

    Prefers a file (immune to shell quoting), then strict JSON, then a lenient
    Python-literal parse (tolerates the single quotes PowerShell often leaves)."""
    if args_file:
        try:
            with open(args_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return (data if isinstance(data, dict) else {}), ""
        except Exception as exc:  # noqa: BLE001
            return {}, f"--args-file could not be read/parsed: {exc}"
    text = (raw or "").strip()
    if not text or text == "{}":
        return {}, ""
    try:
        data = json.loads(text)
        return (data if isinstance(data, dict) else {}), ""
    except json.JSONDecodeError:
        pass
    try:
        import ast
        data = ast.literal_eval(text)
        if isinstance(data, dict):
            return data, ""
        return {}, "--args parsed but is not a JSON object"
    except Exception:  # noqa: BLE001
        return {}, "--args is not valid JSON (the shell likely stripped the quotes)"


if __name__ == "__main__":
    raise SystemExit(main())
