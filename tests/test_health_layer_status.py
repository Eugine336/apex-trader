"""Regression tests for the dashboard health adaptive-layer status.

The health panel must distinguish a component that is OFF by deliberate config
from one that failed to initialise (dormant) and from a running one (active).
See ``dashboard/state_health.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

from dashboard.state_health import (
    _ed_adaptive_layer_detail,
    _ed_adaptive_layers,
    _layer_enabled,
)


def _cfg(*, signal_discovery_enabled=True, virtual_promotion_enabled=True,
         vote_calibration_enabled=True, counterfactual_enabled=True):
    return SimpleNamespace(
        signal_discovery=SimpleNamespace(
            signal_discovery_enabled=signal_discovery_enabled,
            virtual_promotion_enabled=virtual_promotion_enabled,
        ),
        vote_calibrator=SimpleNamespace(
            vote_calibration_enabled=vote_calibration_enabled,
        ),
        counterfactual=SimpleNamespace(
            counterfactual_enabled=counterfactual_enabled,
        ),
    )


def test_disabled_by_config_distinct_from_dormant():
    cfg = _cfg(signal_discovery_enabled=False)
    # signal_discovery present but config-disabled; counterfactual None (not built).
    ctx = SimpleNamespace(
        signal_ledger=object(),
        signal_discovery=object(),
        counterfactual_engine=None,
        vote_calibrator=object(),
        virtual_module_registry=object(),
    )
    layers = _ed_adaptive_layers(ctx, cfg)
    assert layers["signal_discovery"] == "disabled"
    assert layers["counterfactual"] == "dormant"
    assert layers["vote_calibrator"] == "active"
    # no enable flag → presence alone is "active"
    assert layers["signal_ledger"] == "active"


def test_virtual_registry_killed_by_signal_discovery_switch():
    # Even with virtual_promotion on, the signal-discovery kill switch off
    # disables the virtual registry.
    cfg = _cfg(signal_discovery_enabled=False, virtual_promotion_enabled=True)
    ctx = SimpleNamespace(virtual_module_registry=object())
    assert _ed_adaptive_layers(ctx, cfg)["virtual_registry"] == "disabled"
    assert _layer_enabled(cfg, "virtual_registry") is False


def test_detail_carries_reason_and_enabled():
    cfg = _cfg(counterfactual_enabled=True)
    ctx = SimpleNamespace(counterfactual_engine=None, vote_calibrator=object())
    detail = _ed_adaptive_layer_detail(ctx, cfg)
    assert detail["counterfactual"] == {
        "status": "dormant", "reason": "not initialized", "enabled": True,
    }
    assert detail["vote_calibrator"]["status"] == "active"
    assert detail["vote_calibrator"]["enabled"] is True


def test_backward_compatible_without_cfg():
    # Called with no cfg (old signature): present → active, absent → dormant.
    ctx = SimpleNamespace(signal_discovery=object(), counterfactual_engine=None)
    layers = _ed_adaptive_layers(ctx)
    assert layers["signal_discovery"] == "active"
    assert layers["counterfactual"] == "dormant"


def test_empty_ctx_returns_empty():
    assert _ed_adaptive_layers(None, None) == {}
    assert _ed_adaptive_layer_detail(None, None) == {}
