"""
APEX TRADER — Deriv access-token proactive refresh wiring.

Covers the connector's _maybe_refresh_token guard and the PlatformManager
callback that sources a rotated token out-of-band (file or env var).
"""

import threading
import time

from platforms.deriv.deriv_connector import DerivConnector
from platforms.platform_manager import PlatformManager


def _make_connector(callback, token="old", ttl=100.0, elapsed_frac=0.9):
    c = DerivConnector.__new__(DerivConnector)
    c._access_token = token
    c._token_refresh_callback = callback
    c._token_ttl = ttl
    c._token_issued_at = time.time() - ttl * elapsed_frac
    c._token_expires_at = c._token_issued_at + ttl
    c._token_expiry_warned = False
    c._token_lock = threading.Lock()
    return c


class TestMaybeRefreshToken:
    def test_no_callback_is_noop(self):
        c = _make_connector(callback=None)
        assert c._maybe_refresh_token() is False
        assert c._access_token == "old"

    def test_before_threshold_does_not_refresh(self):
        c = _make_connector(callback=lambda: ("new", 100.0), elapsed_frac=0.5)
        assert c._maybe_refresh_token() is False
        assert c._access_token == "old"

    def test_rotates_when_new_token_available(self):
        c = _make_connector(callback=lambda: ("new", 200.0))
        assert c._maybe_refresh_token() is True
        assert c._access_token == "new"

    def test_unchanged_token_does_not_reset_clock(self):
        c = _make_connector(callback=lambda: ("old", 100.0))
        before = c._token_expires_at
        assert c._maybe_refresh_token() is False
        # TTL clock must keep counting down toward the real expiry.
        assert c._token_expires_at == before
        assert c._access_token == "old"

    def test_empty_token_is_ignored(self):
        c = _make_connector(callback=lambda: ("", 100.0))
        assert c._maybe_refresh_token() is False
        assert c._access_token == "old"


class TestPlatformManagerRefreshCallback:
    def _pm(self):
        return PlatformManager.__new__(PlatformManager)

    def test_reads_env_token(self, monkeypatch):
        monkeypatch.delenv("DERIV_ACCESS_TOKEN_FILE", raising=False)
        monkeypatch.setenv("DERIV_ACCESS_TOKEN", "env-token")
        monkeypatch.setenv("DERIV_TOKEN_EXPIRES_IN", "1800")
        token, expires_in = self._pm()._refresh_deriv_token()
        assert token == "env-token"
        assert expires_in == 1800.0

    def test_file_takes_precedence_over_env(self, monkeypatch, tmp_path):
        token_file = tmp_path / "deriv_token"
        token_file.write_text("  file-token\n", encoding="utf-8")
        monkeypatch.setenv("DERIV_ACCESS_TOKEN_FILE", str(token_file))
        monkeypatch.setenv("DERIV_ACCESS_TOKEN", "env-token")
        token, _ = self._pm()._refresh_deriv_token()
        assert token == "file-token"

    def test_falls_back_to_env_when_file_unreadable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DERIV_ACCESS_TOKEN_FILE", str(tmp_path / "missing"))
        monkeypatch.setenv("DERIV_ACCESS_TOKEN", "env-token")
        token, _ = self._pm()._refresh_deriv_token()
        assert token == "env-token"

    def test_defaults_expiry_on_bad_value(self, monkeypatch):
        monkeypatch.delenv("DERIV_ACCESS_TOKEN_FILE", raising=False)
        monkeypatch.setenv("DERIV_ACCESS_TOKEN", "env-token")
        monkeypatch.setenv("DERIV_TOKEN_EXPIRES_IN", "not-a-number")
        _, expires_in = self._pm()._refresh_deriv_token()
        assert expires_in == 3600.0
