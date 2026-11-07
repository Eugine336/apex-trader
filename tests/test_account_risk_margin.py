"""Per-account broker margin guard regression tests."""

from risk.account_risk import AccountRiskManager


def test_margin_guard_does_not_invent_a_breach_without_a_broker_observation():
    risk = AccountRiskManager()
    assert risk.margin_level("mt5:1") is None
    assert risk.margin_entry_blocked("mt5:1") is False
    assert risk.margin_flatten_breached("mt5:1") is False


def test_margin_level_below_entry_guard_blocks_new_exposure():
    risk = AccountRiskManager(margin_block_entry_pct=150.0)
    risk.update_margin_level("mt5:1", 149.9)
    assert risk.margin_entry_blocked("mt5:1") is True
    assert risk.margin_flatten_breached("mt5:1") is False


def test_margin_level_below_flatten_guard_triggers_protective_flatten():
    risk = AccountRiskManager(margin_block_entry_pct=150.0, margin_flatten_pct=100.0)
    risk.update_margin_level("mt5:1", 99.9)
    assert risk.margin_entry_blocked("mt5:1") is True
    assert risk.margin_flatten_breached("mt5:1") is True
    assert risk.flatten_breached("mt5:1") is True


def test_margin_flatten_does_not_depend_on_a_positive_cached_balance():
    risk = AccountRiskManager()
    risk.update_margin_level("mt5:1", 75.0)
    assert risk.balance("mt5:1") == 0.0
    assert risk.flatten_breached("mt5:1") is True


def test_margin_guard_accepts_recovered_margin_level():
    risk = AccountRiskManager()
    risk.update_margin_level("mt5:1", 140.0)
    assert risk.margin_entry_blocked("mt5:1") is True
    risk.update_margin_level("mt5:1", 175.0)
    assert risk.margin_entry_blocked("mt5:1") is False
    assert risk.margin_flatten_breached("mt5:1") is False


def test_disabled_margin_guard_is_inert_but_does_not_erase_observation():
    risk = AccountRiskManager(margin_guardian_enabled=False)
    risk.update_margin_level("mt5:1", 80.0)
    assert risk.margin_level("mt5:1") == 80.0
    assert risk.margin_entry_blocked("mt5:1") is False
    assert risk.margin_flatten_breached("mt5:1") is False


def test_invalid_margin_observation_does_not_replace_last_valid_reading():
    risk = AccountRiskManager()
    risk.update_margin_level("mt5:1", 125.0)
    risk.update_margin_level("mt5:1", float("nan"))
    risk.update_margin_level("mt5:1", float("inf"))
    assert risk.margin_level("mt5:1") == 125.0
