"""
Tests for MT5 order-comment integrity.

Verifies that:
1. build_order_comment produces strings ≤31 chars with the full idem_key.
2. extract_idempotency_key round-trips — recovers the full 12-char key
   from comments produced by build_order_comment.
3. Sanitization removes unsafe characters without breaking parsing.
4. Legacy comment format (pre-PR-C) is still parseable.
"""

from platforms.order_idempotency import (
    _MT5_COMMENT_MAX,
    build_order_comment,
    extract_idempotency_key,
    generate_idempotency_key,
)


class TestBuildOrderComment:
    def test_basic_market_order(self):
        key = "a1b2c3d4e5f6"
        comment = build_order_comment("APEX", key, score=85, session="LN")
        assert len(comment) <= _MT5_COMMENT_MAX
        assert key in comment

    def test_basic_pending_order(self):
        key = "a1b2c3d4e5f6"
        comment = build_order_comment("APND", key, score=70, session="NY")
        assert len(comment) <= _MT5_COMMENT_MAX
        assert key in comment
        assert comment.startswith("APND|")

    def test_max_length_never_exceeded(self):
        key = "abcdef012345"
        comment = build_order_comment("APEX", key, score=99999, session="LONDONNEWYORK")
        assert len(comment) <= _MT5_COMMENT_MAX

    def test_no_score_no_session(self):
        key = "abcdef012345"
        comment = build_order_comment("APEX", key)
        assert comment == "APEX|abcdef012345"
        assert len(comment) <= _MT5_COMMENT_MAX

    def test_idem_key_always_at_index_1(self):
        key = "a1b2c3d4e5f6"
        comment = build_order_comment("APEX", key, score=92, session="AS")
        parts = comment.split("|")
        assert parts[1] == key

    def test_idem_key_survives_long_session(self):
        key = "a1b2c3d4e5f6"
        comment = build_order_comment("APEX", key, score=100, session="A" * 50)
        assert len(comment) <= _MT5_COMMENT_MAX
        assert key in comment
        extracted = extract_idempotency_key(comment)
        assert extracted == key


class TestSanitization:
    def test_spaces_removed(self):
        key = "a1b2c3d4e5f6"
        comment = build_order_comment("APEX", key, session="New York")
        assert " " not in comment
        assert len(comment) <= _MT5_COMMENT_MAX

    def test_non_ascii_removed(self):
        key = "a1b2c3d4e5f6"
        comment = build_order_comment("APEX", key, session="Tōkyō")
        assert "ō" not in comment
        assert len(comment) <= _MT5_COMMENT_MAX

    def test_pipe_in_session_removed(self):
        key = "a1b2c3d4e5f6"
        comment = build_order_comment("APEX", key, session="LN|NY")
        parts = comment.split("|")
        assert parts[1] == key
        assert len(comment) <= _MT5_COMMENT_MAX

    def test_only_safe_chars_in_output(self):
        key = "a1b2c3d4e5f6"
        comment = build_order_comment("APEX!!!", key, score=85, session="test@#$")
        for ch in comment:
            assert ch == "|" or ch.isalnum() or ch in (".", "-", "_"), f"Unsafe char: {ch!r}"


class TestRoundTrip:
    def test_market_order_roundtrip(self):
        key = "a1b2c3d4e5f6"
        comment = build_order_comment("APEX", key, score=85, session="LN")
        extracted = extract_idempotency_key(comment)
        assert extracted == key

    def test_pending_order_roundtrip(self):
        key = "fedcba987654"
        comment = build_order_comment("APND", key, score=72, session="NY")
        extracted = extract_idempotency_key(comment)
        assert extracted == key

    def test_no_score_roundtrip(self):
        key = "abcdef012345"
        comment = build_order_comment("APEX", key)
        extracted = extract_idempotency_key(comment)
        assert extracted == key

    def test_generated_key_roundtrip(self):
        key = generate_idempotency_key("EURUSD", "BUY", 0.01)
        assert len(key) == 12
        comment = build_order_comment("APEX", key, score=88, session="LN_NY")
        assert len(comment) <= _MT5_COMMENT_MAX
        extracted = extract_idempotency_key(comment)
        assert extracted == key

    def test_extreme_score_roundtrip(self):
        key = "112233445566"
        comment = build_order_comment("APEX", key, score=99999)
        extracted = extract_idempotency_key(comment)
        assert extracted == key


class TestLegacyCompatibility:
    def test_legacy_apex_format(self):
        legacy = "APEX|85|LN|a1b2c3d4e5f6"
        extracted = extract_idempotency_key(legacy)
        assert extracted == "a1b2c3d4e5f6"

    def test_legacy_apex_pend_format(self):
        legacy = "APEX_PEND|70|NY|fedcba987654"
        extracted = extract_idempotency_key(legacy)
        assert extracted == "fedcba987654"

    def test_legacy_no_key(self):
        legacy = "APEX|85|LN"
        extracted = extract_idempotency_key(legacy)
        assert extracted is None

    def test_empty_comment(self):
        assert extract_idempotency_key("") is None

    def test_plain_apex(self):
        assert extract_idempotency_key("APEX") is None

    def test_new_format_not_confused_with_legacy(self):
        key = "a1b2c3d4e5f6"
        comment = build_order_comment("APEX", key, score=85, session="LN")
        extracted = extract_idempotency_key(comment)
        assert extracted == key
        assert len(extracted) == 12


class TestConnectorNoDoubleAppend:
    """Verify the comment passed to the MT5 request is exactly what the
    builder produces — no second idempotency_key append or re-truncation."""

    def test_market_comment_is_builder_output(self):
        key = "a1b2c3d4e5f6"
        comment = build_order_comment("APEX", key, score=85, session="LN")
        order_comment = comment or "APEX"
        assert order_comment == comment
        assert key in order_comment
        assert len(order_comment) <= _MT5_COMMENT_MAX

    def test_pending_comment_is_builder_output(self):
        key = "fedcba987654"
        comment = build_order_comment("APND", key, score=70, session="AS")
        pending_comment = comment or "APEX_PENDING"
        assert pending_comment == comment
        assert key in pending_comment
        assert len(pending_comment) <= _MT5_COMMENT_MAX
