"""Canonical owner-limit contract and config synchronization tests."""
import pytest

from config import AppConfig
from risk.owner_limits import OwnerLimits


def test_default_owner_limits_accept_broker_margin_levels_above_100_percent():
    limits = OwnerLimits()
    assert limits.margin_flatten_level_pct == 100.0
    assert limits.margin_block_entry_level_pct == 150.0
    assert limits.margin_warn_level_pct == 200.0


def test_app_config_creates_one_owner_limits_object_and_syncs_legacy_views():
    cfg = AppConfig()
    limits = cfg.owner_limits

    assert isinstance(limits, OwnerLimits)
    assert cfg.risk.risk_per_trade_pct == limits.risk_per_trade_pct
    assert cfg.risk.max_risk_pct_per_trade == limits.max_risk_pct_per_trade
    assert cfg.cognition.origination_risk_fraction == limits.risk_per_trade_pct / 100.0
    assert cfg.risk.max_open_trades == limits.max_open_positions
    assert cfg.governor.max_open_positions == limits.max_open_positions
    assert cfg.governor.max_total_positions == limits.max_open_positions
    assert cfg.risk_management.max_simultaneous_positions == limits.max_open_positions
    assert cfg.risk.max_correlated_trades == limits.max_correlated_positions
    assert cfg.governor.max_correlated_positions == limits.max_correlated_positions
    assert cfg.risk.max_pair_concentration == limits.max_positions_per_symbol
    assert cfg.governor.max_positions_per_symbol == limits.max_positions_per_symbol
    assert cfg.risk_management.max_per_pair_positions == limits.max_positions_per_symbol
    assert cfg.risk.allow_min_lot_over_risk is False


def test_owner_limits_adopts_strictest_legacy_values_without_loosening():
    cfg = AppConfig()
    limits = cfg.owner_limits

    assert limits.max_open_positions <= cfg.governor.max_open_positions
    assert limits.max_open_positions <= cfg.risk.max_open_trades
    assert limits.max_positions_per_symbol <= cfg.governor.max_positions_per_symbol
    assert limits.max_correlated_positions <= cfg.governor.max_correlated_positions
    assert limits.risk_per_trade_pct <= cfg.risk.max_risk_pct_per_trade
    assert limits.daily_loss_limit_pct <= limits.weekly_drawdown_limit_pct
    assert limits.weekly_drawdown_limit_pct <= limits.hard_stop_drawdown_pct


def test_owner_limits_updates_are_atomic_and_propagate_to_legacy_views():
    cfg = AppConfig()
    before = cfg.owner_limits.to_dict()

    with pytest.raises(ValueError):
        cfg.apply_owner_limit_updates({"daily_loss_limit_pct": 99.0})

    assert cfg.owner_limits.to_dict() == before
    assert cfg.risk.max_daily_drawdown_pct == before["daily_loss_limit_pct"]

    cfg.apply_owner_limit_updates({
        "risk_per_trade_pct": 0.5,
        "max_risk_pct_per_trade": 1.5,
        "max_total_risk_pct": 4.0,
        "max_open_positions": 4,
        "max_positions_per_symbol": 1,
        "daily_loss_limit_pct": 2.0,
        "weekly_drawdown_limit_pct": 6.0,
        "hard_stop_drawdown_pct": 12.0,
    })

    assert cfg.risk.risk_per_trade_pct == 0.5
    assert cfg.cognition.origination_risk_fraction == 0.005
    assert cfg.risk.max_risk_pct_per_trade == 1.5
    assert cfg.governor.per_trade_max_risk == 1.5
    assert cfg.risk.max_open_trades == 4
    assert cfg.governor.max_open_positions == 4
    assert cfg.risk_management.max_simultaneous_positions == 4
    assert cfg.risk.max_pair_concentration == 1
    assert cfg.governor.max_positions_per_symbol == 1
    assert cfg.risk_management.max_per_pair_positions == 1
    assert cfg.risk.max_daily_drawdown_pct == 2.0
    assert cfg.governor.daily_loss_cap_pct == 2.0
    assert cfg.risk_management.daily_drawdown_limit_pct == 2.0


def test_owner_limits_reject_non_integral_position_limits():
    limits = OwnerLimits()
    with pytest.raises(ValueError, match="max_open_positions"):
        limits.apply_updates({"max_open_positions": 2.5})


def test_owner_limits_reject_margin_thresholds_that_can_never_warn_before_block():
    limits = OwnerLimits()
    with pytest.raises(ValueError, match="margin levels"):
        limits.apply_updates({
            "margin_warn_level_pct": 100.0,
            "margin_block_entry_level_pct": 150.0,
        })


def test_owner_limits_cannot_weaken_emergency_drawdown_protection():
    limits = OwnerLimits()
    with pytest.raises(ValueError, match="15% hard-stop ceiling"):
        limits.apply_updates({"hard_stop_drawdown_pct": 16.0})
    with pytest.raises(ValueError, match="cannot exceed hard_stop_drawdown_pct"):
        limits.apply_updates({"daily_loss_flatten_pct": 16.0})


def test_owner_limits_cannot_disable_broker_margin_protection():
    limits = OwnerLimits()
    with pytest.raises(ValueError, match="cannot be disabled"):
        limits.apply_updates({"margin_guardian_enabled": False})


def test_owner_limits_cannot_weaken_broker_margin_protection():
    limits = OwnerLimits()
    for field, value, message in (
        ("margin_flatten_level_pct", 99.0, "100% stop-out guard"),
        ("margin_block_entry_level_pct", 149.0, "150% entry guard"),
        ("margin_warn_level_pct", 199.0, "200% warning guard"),
    ):
        with pytest.raises(ValueError, match=message):
            limits.apply_updates({field: value})


def test_owner_limits_allow_stricter_margin_and_drawdown_protection():
    limits = OwnerLimits()
    limits.apply_updates({
        "hard_stop_drawdown_pct": 12.0,
        "weekly_drawdown_limit_pct": 6.0,
        "daily_loss_limit_pct": 2.0,
        "daily_loss_flatten_pct": 4.0,
        "margin_flatten_level_pct": 120.0,
        "margin_block_entry_level_pct": 180.0,
        "margin_warn_level_pct": 250.0,
    })
    assert limits.hard_stop_drawdown_pct == 12.0
    assert limits.margin_flatten_level_pct == 120.0

def test_zero_campaign_exposure_is_preserved_when_adopting_legacy_config():
    cfg = AppConfig()
    limits = OwnerLimits.from_legacy(
        risk=cfg.risk,
        governor=cfg.governor,
        cognition=cfg.cognition,
        risk_management=cfg.risk_management,
        brain_max_total_exposure=0.0,
    )
    assert limits.max_campaign_exposure_units == 0.0


def test_owner_limit_updates_are_isolated_between_app_config_instances():
    first = AppConfig()
    second = AppConfig()
    first.apply_owner_limit_updates({
        "risk_per_trade_pct": 0.25,
        "max_risk_pct_per_trade": 1.5,
        "max_total_risk_pct": 4.0,
    })
    assert first.owner_limits.risk_per_trade_pct == 0.25
    assert second.owner_limits.risk_per_trade_pct == 0.75
    assert second.risk.risk_per_trade_pct == 0.75


def test_owner_limits_reject_fractional_broker_position_caps():
    limits = OwnerLimits()
    with pytest.raises(ValueError, match="max_broker_positions"):
        limits.apply_updates({"max_broker_positions": 1.5})


def test_owner_limits_preserve_hard_per_trade_risk_ceiling():
    limits = OwnerLimits()
    with pytest.raises(ValueError, match="5% hard per-trade ceiling"):
        limits.apply_updates({
            "risk_per_trade_pct": 6.0,
            "max_risk_pct_per_trade": 6.0,
            "max_total_risk_pct": 8.0,
        })


def test_owner_limits_reject_boolean_numeric_values():
    limits = OwnerLimits()
    with pytest.raises(ValueError, match="not a boolean"):
        limits.apply_updates({"risk_per_trade_pct": True})

@pytest.mark.parametrize(
    "field,value",
    [
        ("daily_loss_flatten_enabled", False),
        ("margin_guardian_enabled", False),
        ("graded_exposure_enabled", False),
        ("correlation_threshold", 0.2),
        ("cooldown_sizing_factor", 1.0),
    ],
)
def test_owner_updates_cannot_change_internal_safety_or_analysis_settings(field, value):
    limits = OwnerLimits()
    with pytest.raises(ValueError, match="internal safety/analysis"):
        limits.apply_updates({field: value})


def test_owner_serialization_excludes_internal_safety_and_analysis_settings():
    payload = OwnerLimits().to_owner_dict()
    assert "daily_loss_flatten_enabled" not in payload
    assert "margin_guardian_enabled" not in payload
    assert "graded_exposure_enabled" not in payload
    assert "correlation_threshold" not in payload
    assert "cooldown_sizing_factor" not in payload
    assert "risk_per_trade_pct" in payload
    assert "max_total_risk_pct" in payload
