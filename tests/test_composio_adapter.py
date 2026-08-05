"""Tests for the Composio adapter's v3 REST migration (request + response shape)."""

from action.composio import ComposioAdapter, build_adapter


class _CaptureTransport:
    """Fake transport: records the last request and returns a canned response."""

    def __init__(self, status=200, text='{"successful": true, "data": {}}'):
        self.status = status
        self.text = text
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        import json
        self.calls.append({
            "url": url, "headers": dict(headers),
            "body": json.loads(body.decode("utf-8")), "timeout": timeout,
        })
        return self.status, self.text


def test_v3_request_url_and_body_shape():
    tr = _CaptureTransport()
    a = ComposioAdapter("k", entity_id="acct-1", transport=tr)  # default v3
    a.execute("COMPOSIO_SEARCH_SEARCH", {"query": "gold"})
    call = tr.calls[-1]
    assert call["url"].endswith("/api/v3/tools/execute/COMPOSIO_SEARCH_SEARCH")
    assert call["body"] == {"user_id": "acct-1", "arguments": {"query": "gold"}}
    assert call["headers"]["x-api-key"] == "k"


def test_v3_response_parsed_with_payload():
    tr = _CaptureTransport(text='{"successful": true, "data": {"items": [{"title": "t"}]}}')
    a = ComposioAdapter("k", transport=tr)
    res = a.execute("COMPOSIO_SEARCH_SEARCH", {"query": "x"})
    assert res.ok is True
    assert res.data.get("items") == [{"title": "t"}]


def test_v3_error_field_marks_failure():
    tr = _CaptureTransport(text='{"successful": false, "error": "tool blew up"}')
    a = ComposioAdapter("k", transport=tr)
    res = a.execute("SLACK_SEND_MESSAGE", {"message": "hi"})
    assert res.ok is False
    assert "tool blew up" in res.detail


def test_http_410_is_a_failed_result_not_a_raise():
    tr = _CaptureTransport(status=410, text='{"error":"upgrade to v3"}')
    a = ComposioAdapter("k", transport=tr)
    res = a.execute("OPERATOR_NOTIFY", {})
    assert res.ok is False
    assert "HTTP 410" in res.detail


def test_legacy_v2_shape_is_available_as_fallback():
    tr = _CaptureTransport()
    a = ComposioAdapter("k", entity_id="acct-1", api_version="v2", transport=tr)
    a.execute("GITHUB_CREATE_AN_ISSUE", {"title": "t", "body": "b"})
    call = tr.calls[-1]
    assert call["url"].endswith("/api/v2/actions/GITHUB_CREATE_AN_ISSUE/execute")
    assert call["body"] == {"entityId": "acct-1", "input": {"title": "t", "body": "b"}}


def test_build_adapter_threads_api_version():
    from types import SimpleNamespace
    cfg = SimpleNamespace(enabled=True, dry_run=False, api_key="k",
                          base_url="https://backend.composio.dev",
                          entity_id="default", timeout_seconds=20.0, api_version="v3")
    tr = _CaptureTransport()
    a = build_adapter(cfg, transport=tr)
    a.execute("COMPOSIO_SEARCH_SEARCH", {"query": "q"})
    assert tr.calls[-1]["url"].endswith("/api/v3/tools/execute/COMPOSIO_SEARCH_SEARCH")
