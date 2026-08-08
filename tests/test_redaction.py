"""Regression tests for log redaction of broker account identifiers (L10).

Pure-stdlib logic — runs anywhere the project imports cleanly.
"""

from __future__ import annotations

from ops.redaction import mask_account_id, redact_account_in_url


class TestMaskAccountId:
    def test_masks_numeric_login_keeping_last_four(self):
        assert mask_account_id(52856215) == "****6215"

    def test_masks_alphanumeric_account(self):
        assert mask_account_id("DOT92986948") == "*******6948"

    def test_short_value_fully_masked(self):
        # Values no longer than keep_last must not leak via the visible tail.
        assert mask_account_id("123") == "***"
        assert mask_account_id("12") == "**"

    def test_none_and_empty_return_unknown(self):
        assert mask_account_id(None) == "unknown"
        assert mask_account_id("") == "unknown"
        assert mask_account_id("   ") == "unknown"

    def test_keep_last_zero_fully_masks(self):
        assert mask_account_id("12345678", keep_last=0) == "********"

    def test_original_value_never_present_in_output(self):
        raw = "52856215"
        assert raw not in mask_account_id(raw)


class TestRedactAccountInUrl:
    def test_masks_account_segment_in_otp_url(self):
        url = "https://api.derivws.com/trading/v1/options/accounts/DOT92986948/otp"
        expected = "https://api.derivws.com/trading/v1/options/accounts/*******6948/otp"
        assert redact_account_in_url(url) == expected

    def test_url_without_account_segment_unchanged(self):
        url = "https://api.derivws.com/v1/health"
        assert redact_account_in_url(url) == url

    def test_full_account_id_not_present_in_redacted_url(self):
        acct = "DOT92986948"
        url = f"https://api.derivws.com/trading/v1/options/accounts/{acct}/otp"
        assert acct not in redact_account_in_url(url)
