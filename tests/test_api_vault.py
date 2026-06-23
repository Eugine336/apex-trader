"""Tests for the broker credential vault (encryption + masking + validation)."""

import pytest

from api.broker_vault import (
    BROKER_DERIV,
    BROKER_MT5,
    BrokerVault,
    VaultError,
    mask_credentials,
)

_MASTER = "test-master-key-0123456789abcdef"


def test_encrypt_decrypt_roundtrip_mt5():
    vault = BrokerVault(_MASTER)
    creds = {"login": "12345678", "password": "s3cret", "server": "Broker-Live"}
    token = vault.encrypt(1, BROKER_MT5, creds)
    assert token and token != "s3cret"
    out = vault.decrypt(1, token)
    assert out["login"] == 12345678  # normalised to int
    assert out["password"] == "s3cret"
    assert out["server"] == "Broker-Live"


def test_encrypt_decrypt_roundtrip_deriv():
    vault = BrokerVault(_MASTER)
    creds = {"access_token": "ory_at_abc", "app_id": "1234", "account_type": "demo"}
    token = vault.encrypt(7, BROKER_DERIV, creds)
    out = vault.decrypt(7, token)
    assert out["access_token"] == "ory_at_abc"
    assert out["app_id"] == "1234"
    assert out["account_type"] == "demo"


def test_per_user_key_isolation():
    """Ciphertext for user A must not decrypt under user B's derived key."""
    vault = BrokerVault(_MASTER)
    token = vault.encrypt(1, BROKER_MT5, {"login": "1", "password": "p", "server": "s"})
    with pytest.raises(VaultError):
        vault.decrypt(2, token)


def test_wrong_master_key_fails():
    token = BrokerVault(_MASTER).encrypt(
        1, BROKER_MT5, {"login": "1", "password": "p", "server": "s"}
    )
    with pytest.raises(VaultError):
        BrokerVault("a-different-master-key").decrypt(1, token)


def test_validate_rejects_missing_fields():
    vault = BrokerVault(_MASTER)
    with pytest.raises(VaultError):
        vault.encrypt(1, BROKER_MT5, {"login": "1", "password": "p"})  # no server
    with pytest.raises(VaultError):
        vault.encrypt(1, BROKER_DERIV, {"app_id": "1"})  # no access_token


def test_validate_rejects_unknown_broker():
    with pytest.raises(VaultError):
        BrokerVault.validate("oanda", {"x": 1})


def test_validate_rejects_non_int_login():
    with pytest.raises(VaultError):
        BrokerVault.validate(BROKER_MT5, {"login": "abc", "password": "p", "server": "s"})


def test_empty_master_key_rejected():
    with pytest.raises(VaultError):
        BrokerVault("")


def test_mask_credentials_hides_secrets():
    masked = mask_credentials(
        BROKER_MT5, {"login": 12345678, "password": "supersecret", "server": "Broker"}
    )
    assert masked["login"] == 12345678
    assert masked["server"] == "Broker"
    assert masked["password"].startswith("****")
    assert "supersecret" not in masked["password"]
    assert masked["broker_type"] == BROKER_MT5


def test_mask_short_secret():
    masked = mask_credentials(BROKER_DERIV, {"access_token": "ab", "app_id": "1"})
    assert masked["access_token"] == "****"
