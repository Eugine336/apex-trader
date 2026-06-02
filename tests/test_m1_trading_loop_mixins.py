"""
M1 TradingLoop mixin decomposition — MRO and method-resolution tests.

These tests verify that the TradingLoop class correctly inherits all
methods from its three mixin classes and that no method was lost or
renamed during the extraction.  They operate at class level only
(no TradingLoop instantiation, which requires live brokers).
"""

from platforms.trading_loop.recovery_mixin import RecoveryReconciliationMixin
from platforms.trading_loop.risk_heat_mixin import RiskHeatMarginMixin
from platforms.trading_loop.exit_checks_mixin import ExitChecksMixin
from platforms.main_loop import TradingLoop


RECOVERY_METHODS = [
    "_perform_startup_recovery",
    "_restore_positions",
    "_reconcile_positions",
    "_reconcile_externally_closed",
    "_check_and_reconnect",
]

RISK_HEAT_METHODS = [
    "_check_portfolio_heat",
    "_apply_defensive_actions",
    "_apply_reduction_actions",
    "_apply_emergency_actions",
    "_get_margin_level",
    "_check_margin_for_entry",
    "_margin_guardian_check",
    "_emergency_flatten_all",
]

EXIT_CHECK_METHODS = [
    "_check_invalidation",
    "_check_conviction_collapse",
    "_check_htf_candle_close",
    "_apply_dynamic_sl_tightening",
    "_check_news_exit",
    "_check_session_close",
    "_check_spread_deterioration",
]

ALL_MIXIN_METHODS = RECOVERY_METHODS + RISK_HEAT_METHODS + EXIT_CHECK_METHODS

LIFECYCLE_METHODS = [
    "__init__",
    "run",
    "run_once",
    "stop",
    "_scan_and_enter",
    "_execute_entry",
    "_update_positions",
]


def test_recovery_mixin_in_mro():
    assert RecoveryReconciliationMixin in TradingLoop.__mro__


def test_risk_heat_mixin_in_mro():
    assert RiskHeatMarginMixin in TradingLoop.__mro__


def test_exit_checks_mixin_in_mro():
    assert ExitChecksMixin in TradingLoop.__mro__


def test_all_mixin_methods_present_on_trading_loop():
    for method_name in ALL_MIXIN_METHODS:
        attr = getattr(TradingLoop, method_name, None)
        assert attr is not None, f"Method {method_name} missing from TradingLoop"
        assert callable(attr), f"{method_name} is not callable"


def test_recovery_methods_defined_on_recovery_mixin():
    for method_name in RECOVERY_METHODS:
        attr = getattr(TradingLoop, method_name)
        assert (
            "RecoveryReconciliationMixin" in attr.__qualname__
        ), f"{method_name} not defined on RecoveryReconciliationMixin (qualname={attr.__qualname__})"


def test_risk_heat_methods_defined_on_risk_heat_mixin():
    for method_name in RISK_HEAT_METHODS:
        attr = getattr(TradingLoop, method_name)
        assert (
            "RiskHeatMarginMixin" in attr.__qualname__
        ), f"{method_name} not defined on RiskHeatMarginMixin (qualname={attr.__qualname__})"


def test_exit_check_methods_defined_on_exit_checks_mixin():
    for method_name in EXIT_CHECK_METHODS:
        attr = getattr(TradingLoop, method_name)
        assert (
            "ExitChecksMixin" in attr.__qualname__
        ), f"{method_name} not defined on ExitChecksMixin (qualname={attr.__qualname__})"


def test_lifecycle_methods_remain_on_trading_loop():
    for method_name in LIFECYCLE_METHODS:
        attr = getattr(TradingLoop, method_name, None)
        assert attr is not None, f"Lifecycle method {method_name} missing from TradingLoop"
        if method_name != "__init__":
            assert (
                "TradingLoop" in attr.__qualname__
            ), f"Lifecycle method {method_name} was moved off TradingLoop (qualname={attr.__qualname__})"


def test_no_method_overlap_between_mixins():
    recovery_set = set(RECOVERY_METHODS)
    risk_set = set(RISK_HEAT_METHODS)
    exit_set = set(EXIT_CHECK_METHODS)
    assert not (recovery_set & risk_set), "Overlap between recovery and risk mixins"
    assert not (recovery_set & exit_set), "Overlap between recovery and exit mixins"
    assert not (risk_set & exit_set), "Overlap between risk and exit mixins"


def test_total_method_count_preserved():
    expected_count = 46
    methods = [
        name
        for name in dir(TradingLoop)
        if not name.startswith("__") or name == "__init__"
    ]
    method_count = sum(
        1
        for name in methods
        if callable(getattr(TradingLoop, name, None))
        and not isinstance(getattr(TradingLoop, name, None), property)
    )
    assert method_count >= expected_count, (
        f"Expected at least {expected_count} methods on TradingLoop, found {method_count}"
    )
