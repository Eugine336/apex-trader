"""Tests for the Composio MCP client adapter (JSON-RPC over Streamable HTTP)."""

import json

from action.mcp_client import McpActionAdapter, _extract_jsonrpc_messages


class _FakeMcp:
    """Scripted MCP transport: records requests, returns canned responses.

    ``responses`` maps a JSON-RPC method → (status, headers, body_text). A
    missing method falls back to a 202 empty (notification) reply.
    """

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        msg = json.loads(body.decode("utf-8"))
        method = msg.get("method", "")
        self.calls.append({"url": url, "headers": dict(headers), "msg": msg})
        status, hdrs, text = self.responses.get(method, (202, {}, ""))
        # Echo the request id into the canned result body when it has a slot.
        if text and "{id}" in text:
            text = text.replace("{id}", str(msg.get("id", 1)))
        return status, hdrs, text


def _result_json(structured=None, text=None, is_error=False):
    result = {"isError": is_error, "content": []}
    if structured is not None:
        result["structuredContent"] = structured
    if text is not None:
        result["content"] = [{"type": "text", "text": text}]
    return json.dumps({"jsonrpc": "2.0", "id": "{id}", "result": result})


def _init_response():
    return (200, {"mcp-session-id": "sess-123", "content-type": "application/json"},
            json.dumps({"jsonrpc": "2.0", "id": "{id}", "result": {"protocolVersion": "2025-06-18"}}))


def _tools_list_response(names):
    tools = [{"name": n} for n in names]
    return (200, {"content-type": "application/json"},
            json.dumps({"jsonrpc": "2.0", "id": "{id}", "result": {"tools": tools}}))


def test_initialize_handshake_captures_session_and_sends_initialized():
    tr = _FakeMcp({
        "initialize": _init_response(),
        # Advertise the tool directly so it is called by name (no routing).
        "tools/list": _tools_list_response(["COMPOSIO_SEARCH_SEARCH"]),
        "tools/call": (200, {"content-type": "application/json"},
                       _result_json(structured={"items": [{"title": "t"}]})),
    })
    a = McpActionAdapter("ck_key", transport=tr)
    res = a.execute("COMPOSIO_SEARCH_SEARCH", {"query": "gold"})
    methods = [c["msg"]["method"] for c in tr.calls]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    # session id echoed back on the tools/call request
    call = next(c for c in tr.calls if c["msg"]["method"] == "tools/call")
    assert call["headers"]["mcp-session-id"] == "sess-123"
    assert call["headers"]["x-consumer-api-key"] == "ck_key"
    assert res.ok is True
    assert res.data.get("items") == [{"title": "t"}]


def test_app_action_is_routed_through_executor_meta_tool():
    """When the server only advertises the generic meta-tools, an app-action
    must be dispatched through COMPOSIO_MULTI_EXECUTE_TOOL — not called by name."""
    tr = _FakeMcp({
        "initialize": _init_response(),
        "tools/list": _tools_list_response([
            "COMPOSIO_MULTI_EXECUTE_TOOL", "COMPOSIO_SEARCH_TOOLS",
        ]),
        "tools/call": (200, {"content-type": "application/json"},
                       _result_json(structured={"ok": True})),
    })
    a = McpActionAdapter("k", transport=tr)
    res = a.execute("SLACK_SEND_MESSAGE", {"message": "hi"})
    call = next(c for c in tr.calls if c["msg"]["method"] == "tools/call")
    params = call["msg"]["params"]
    assert params["name"] == "COMPOSIO_MULTI_EXECUTE_TOOL"
    # Confirmed live shape: top-level "tools" array of {tool_slug, arguments}.
    assert params["arguments"] == {
        "tools": [{"tool_slug": "SLACK_SEND_MESSAGE", "arguments": {"message": "hi"}}]
    }
    assert res.ok is True


def test_router_shape_is_configurable():
    tr = _FakeMcp({
        "initialize": _init_response(),
        "tools/list": _tools_list_response(["RUN_TOOL"]),
        "tools/call": (200, {"content-type": "application/json"}, _result_json(structured={})),
    })
    a = McpActionAdapter(
        "k", transport=tr, router_tool="RUN_TOOL",
        router_tools_key="tools", router_slug_key="slug", router_args_key="input",
    )
    a.execute("GITHUB_CREATE_AN_ISSUE", {"title": "x"})
    call = next(c for c in tr.calls if c["msg"]["method"] == "tools/call")
    params = call["msg"]["params"]
    assert params["name"] == "RUN_TOOL"
    assert params["arguments"] == {
        "tools": [{"slug": "GITHUB_CREATE_AN_ISSUE", "input": {"title": "x"}}]
    }


def test_directly_advertised_tool_is_called_by_name():
    tr = _FakeMcp({
        "initialize": _init_response(),
        "tools/list": _tools_list_response([
            "COMPOSIO_MULTI_EXECUTE_TOOL", "COMPOSIO_SEARCH_TOOLS",
        ]),
        "tools/call": (200, {"content-type": "application/json"}, _result_json(structured={})),
    })
    a = McpActionAdapter("k", transport=tr)
    # A meta-tool the server DOES advertise is invoked directly, never wrapped.
    a.execute("COMPOSIO_SEARCH_TOOLS", {"q": "slack"})
    call = next(c for c in tr.calls if c["msg"]["method"] == "tools/call")
    assert call["msg"]["params"] == {"name": "COMPOSIO_SEARCH_TOOLS", "arguments": {"q": "slack"}}


def test_list_tools_returns_advertised_names():
    tr = _FakeMcp({
        "initialize": _init_response(),
        "tools/list": _tools_list_response(["B_TOOL", "A_TOOL"]),
    })
    a = McpActionAdapter("k", transport=tr)
    assert a.list_tools() == ["A_TOOL", "B_TOOL"]


def test_tools_call_arguments_and_name_shape():
    tr = _FakeMcp({"initialize": _init_response(),
                   "tools/call": (200, {"content-type": "application/json"}, _result_json(structured={}))})
    a = McpActionAdapter("k", transport=tr)
    a.execute("SLACK_SEND_MESSAGE", {"message": "hi"})
    call = next(c for c in tr.calls if c["msg"]["method"] == "tools/call")
    assert call["msg"]["params"] == {"name": "SLACK_SEND_MESSAGE", "arguments": {"message": "hi"}}


def test_text_content_json_becomes_payload():
    tr = _FakeMcp({"initialize": _init_response(),
                   "tools/call": (200, {"content-type": "application/json"},
                                  _result_json(text=json.dumps({"answer": "range-bound"})))})
    a = McpActionAdapter("k", transport=tr)
    res = a.execute("ADVISOR_CONSULT", {"query": "x"})
    assert res.ok is True and res.data.get("answer") == "range-bound"


def test_plain_text_content_wrapped_as_answer():
    tr = _FakeMcp({"initialize": _init_response(),
                   "tools/call": (200, {"content-type": "application/json"},
                                  _result_json(text="just some prose"))})
    a = McpActionAdapter("k", transport=tr)
    res = a.execute("ADVISOR_CONSULT", {"query": "x"})
    assert res.ok is True and res.data.get("answer") == "just some prose"


def test_sse_response_is_parsed():
    sse = ("event: message\n"
           "data: " + _result_json(structured={"items": [{"title": "sse"}]}) + "\n\n")
    tr = _FakeMcp({"initialize": _init_response(),
                   "tools/call": (200, {"content-type": "text/event-stream"}, sse)})
    a = McpActionAdapter("k", transport=tr)
    res = a.execute("COMPOSIO_SEARCH_SEARCH", {"query": "x"})
    assert res.ok is True and res.data.get("items") == [{"title": "sse"}]


def test_tool_error_flag_marks_failure():
    tr = _FakeMcp({"initialize": _init_response(),
                   "tools/call": (200, {"content-type": "application/json"},
                                  _result_json(text="boom", is_error=True))})
    a = McpActionAdapter("k", transport=tr)
    res = a.execute("SLACK_SEND_MESSAGE", {})
    assert res.ok is False


def test_jsonrpc_error_marks_failure():
    err = json.dumps({"jsonrpc": "2.0", "id": "{id}", "error": {"code": -32601, "message": "no such tool"}})
    tr = _FakeMcp({"initialize": _init_response(),
                   "tools/call": (200, {"content-type": "application/json"}, err)})
    a = McpActionAdapter("k", transport=tr)
    res = a.execute("NOPE", {})
    assert res.ok is False and "no such tool" in res.detail


def test_session_is_reused_across_calls():
    tr = _FakeMcp({"initialize": _init_response(),
                   "tools/call": (200, {"content-type": "application/json"}, _result_json(structured={}))})
    a = McpActionAdapter("k", transport=tr)
    a.execute("A", {})
    a.execute("B", {})
    # initialize only once; two tools/call
    assert [c["msg"]["method"] for c in tr.calls].count("initialize") == 1
    assert [c["msg"]["method"] for c in tr.calls].count("tools/call") == 2


def test_initialize_http_failure_is_safe():
    tr = _FakeMcp({"initialize": (500, {}, "boom")})
    a = McpActionAdapter("k", transport=tr)
    res = a.execute("A", {})
    assert res.ok is False and "initialize failed" in res.detail


def test_http_4xx_resets_session_for_retry():
    tr = _FakeMcp({"initialize": _init_response(),
                   "tools/call": (401, {}, "unauthorized")})
    a = McpActionAdapter("k", transport=tr)
    res = a.execute("A", {})
    assert res.ok is False
    assert a._initialized is False and a._session_id == ""  # reset so next call re-handshakes


def test_no_api_key_is_unusable():
    a = McpActionAdapter("", transport=_FakeMcp({}))
    assert a.usable is False
    assert a.execute("A", {}).ok is False


def test_extract_messages_handles_batch_and_garbage():
    assert _extract_jsonrpc_messages("", "application/json") == []
    assert _extract_jsonrpc_messages("not json", "application/json") == []
    batch = json.dumps([{"jsonrpc": "2.0", "id": 1, "result": {}}, {"x": 1}])
    assert len(_extract_jsonrpc_messages(batch, "application/json")) == 2
