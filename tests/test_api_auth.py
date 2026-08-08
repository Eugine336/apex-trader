"""Tests for auth primitives: password hashing, JWT tokens, rate limiting."""

import time

import pytest
from jose import JWTError

from api.auth import (
    TOKEN_ACCESS,
    TOKEN_REFRESH,
    TOKEN_RESET,
    RateLimiter,
    create_access_token,
    create_refresh_token,
    create_reset_token,
    decode_token,
    hash_password,
    verify_password,
)
from api.config import ApiConfig


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_API_DATA_DIR", str(tmp_path / "api_data"))
    monkeypatch.setenv("APEX_API_JWT_SECRET", "unit-test-jwt-secret")
    return ApiConfig()


def test_password_hash_roundtrip():
    h = hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong", h)


def test_verify_handles_garbage_hash():
    assert not verify_password("x", "not-a-real-hash")


def test_access_token_roundtrip(config):
    token = create_access_token(42, config)
    claims = decode_token(token, TOKEN_ACCESS, config)
    assert int(claims["sub"]) == 42
    assert claims["type"] == TOKEN_ACCESS


def test_refresh_and_reset_token_types(config):
    rt = create_refresh_token(5, config)
    assert int(decode_token(rt, TOKEN_REFRESH, config)["sub"]) == 5
    pt = create_reset_token(5, config)
    assert int(decode_token(pt, TOKEN_RESET, config)["sub"]) == 5


def test_token_type_mismatch_rejected(config):
    """A refresh token must not validate as an access token."""
    rt = create_refresh_token(1, config)
    with pytest.raises(JWTError):
        decode_token(rt, TOKEN_ACCESS, config)


def test_token_bad_signature_rejected(config):
    token = create_access_token(1, config)
    config.jwt_secret = "rotated-secret"
    with pytest.raises(JWTError):
        decode_token(token, TOKEN_ACCESS, config)


def test_rate_limiter_blocks_after_max():
    rl = RateLimiter(max_attempts=3, window_seconds=60)
    assert rl.check("ip") is True
    assert rl.check("ip") is True
    assert rl.check("ip") is True
    assert rl.check("ip") is False  # 4th blocked
    # A different key has its own budget.
    assert rl.check("other") is True


def test_rate_limiter_window_recovery():
    rl = RateLimiter(max_attempts=1, window_seconds=1)
    assert rl.check("ip") is True
    assert rl.check("ip") is False
    time.sleep(1.1)
    assert rl.check("ip") is True
