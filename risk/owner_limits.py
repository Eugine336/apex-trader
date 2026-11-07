"""Canonical, validated owner-controlled trading risk limits.

This module deliberately has no dependency on application configuration. The
AppConfig layer adapts legacy config fields into one OwnerLimits instance and
synchronizes legacy views from that canonical object.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, fields, replace
from typing import Any, Mapping


def _finite(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _get(obj: Any, name: str, default: Any) -> Any:
    return getattr(obj, name, default) if obj is not None else default


def _positive_min(*values: Any, default: float) -> float:
    parsed = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(number) and number > 0:
            parsed.append(number)
    return min(parsed) if parsed else float(default)


@dataclass
class OwnerLimits:
    """Single source of truth for owner-set capital and portfolio constraints.

    Percentages are percentage points of account equity (e.g. 0.75 means 0.75%).
    Exposure units are normalized portfolio exposure units, not percentages.
    A zero max_broker_positions means no additional broker-specific cap.
    """

    risk_per_trade_pct: float = 0.75
    max_risk_pct_per_trade: float = 2.0
    max_total_risk_pct: float = 6.0
    max_open_positions: int = 5
    max_positions_per_symbol: int = 2
    max_correlated_positions: int = 2
    max_cluster_same_direction: int = 3
    max_broker_positions: int = 0
    max_currency_exposure: int = 3
    max_sector_exposure: int = 4
    max_positions_per_timeframe_class: int = 3
    max_directional_exposure_pct: float = 60.0
    max_regime_exposure_pct: float = 40.0
    max_campaign_exposure_units: float = 3.0
    max_campaign_exposure_fraction: float = 1.0
    hedge_ratio_cap: float = 0.30
    allow_intentional_hedge: bool = False
    daily_loss_limit_pct: float = 3.0
    weekly_drawdown_limit_pct: float = 8.0
    hard_stop_drawdown_pct: float = 15.0
    daily_loss_recovery_pct: float = 1.5
    daily_loss_flatten_enabled: bool = True
    daily_loss_flatten_pct: float = 5.0
    portfolio_heat_block_pct: float = 2.0
    margin_guardian_enabled: bool = True
    margin_warn_level_pct: float = 200.0
    margin_block_entry_level_pct: float = 150.0
    margin_flatten_level_pct: float = 100.0
    graded_exposure_enabled: bool = True
    cooldown_hours: float = 4.0
    cooldown_sizing_factor: float = 0.50
    max_correlated_exposure_factor: float = 1.50
    correlation_threshold: float = 0.70

    def __post_init__(self) -> None:
        self.validate()

    @classmethod
    def from_legacy(
        cls,
        *,
        risk: Any,
        governor: Any,
        cognition: Any,
        risk_management: Any,
        brain_max_correlated_positions: int = 3,
        brain_max_total_exposure: float = 3.0,
        position_sizer_engine_cap_pct: float = 2.5,
    ) -> "OwnerLimits":
        """Adopt the strictest existing effective values without loosening them."""
        risk_pct = _positive_min(
            _get(risk, "risk_per_trade_pct", 0.75),
            _finite(_get(cognition, "origination_risk_fraction", 0.01), "origination_risk_fraction") * 100.0,
            default=0.75,
        )
        max_open = int(_positive_min(
            _get(risk, "max_open_trades", 5),
            _get(governor, "max_open_positions", 8),
            _get(governor, "max_total_positions", 10),
            _get(risk_management, "max_simultaneous_positions", 10),
            default=5,
        ))
        max_pair = int(_positive_min(
            _get(governor, "max_positions_per_symbol", 2),
            _get(risk_management, "max_per_pair_positions", 2),
            _get(risk, "max_pair_concentration", 0),
            default=2,
        ))
        max_corr = int(_positive_min(
            _get(risk, "max_correlated_trades", 2),
            _get(governor, "max_correlated_positions", 2),
            brain_max_correlated_positions,
            default=2,
        ))
        daily_limit = _positive_min(
            _get(risk, "max_daily_drawdown_pct", 3.0),
            _get(governor, "daily_loss_cap_pct", 3.0),
            _get(risk_management, "daily_drawdown_limit_pct", 3.0),
            default=3.0,
        )
        weekly_limit = _positive_min(
            _get(risk, "max_weekly_drawdown_pct", 8.0),
            _get(risk_management, "rolling_drawdown_limit_pct", 8.0),
            default=8.0,
        )
        max_trade_cap = _positive_min(
            _get(risk, "max_risk_pct_per_trade", 5.0),
            _get(governor, "per_trade_max_risk", 2.0),
            position_sizer_engine_cap_pct,
            default=2.0,
        )
        return cls(
            risk_per_trade_pct=min(risk_pct, max_trade_cap),
            max_risk_pct_per_trade=max_trade_cap,
            max_total_risk_pct=_positive_min(
                _get(governor, "max_total_risk_pct", 6.0), default=6.0
            ),
            max_open_positions=max_open,
            max_positions_per_symbol=max_pair,
            max_correlated_positions=max_corr,
            max_cluster_same_direction=int(_positive_min(
                _get(risk, "max_cluster_same_direction", 3), default=3
            )),
            max_broker_positions=max(0, int(_get(risk, "max_broker_positions", 0) or 0)),
            max_currency_exposure=int(_positive_min(
                _get(governor, "max_currency_exposure", 3), default=3
            )),
            max_sector_exposure=int(_positive_min(
                _get(governor, "max_sector_exposure", 4), default=4
            )),
            max_positions_per_timeframe_class=int(_positive_min(
                _get(governor, "max_positions_per_tf_class", 3), default=3
            )),
            max_directional_exposure_pct=_positive_min(
                _get(risk_management, "max_directional_exposure_pct", 60.0), default=60.0
            ),
            max_regime_exposure_pct=_positive_min(
                _get(risk_management, "max_per_regime_pct", 40.0), default=40.0
            ),
            max_campaign_exposure_units=_positive_min(
                brain_max_total_exposure, default=3.0
            ),
            max_campaign_exposure_fraction=min(1.0, max(0.0, _finite(
                _get(cognition, "origination_max_exposure", 1.0),
                "origination_max_exposure",
            ))),
            hedge_ratio_cap=_finite(_get(governor, "hedge_ratio_cap", 0.30), "hedge_ratio_cap"),
            allow_intentional_hedge=bool(_get(risk, "allow_intentional_hedge", False)),
            daily_loss_limit_pct=daily_limit,
            weekly_drawdown_limit_pct=weekly_limit,
            hard_stop_drawdown_pct=_positive_min(
                _get(risk_management, "hard_stop_drawdown_pct", 15.0), default=15.0
            ),
            daily_loss_recovery_pct=_finite(
                _get(governor, "daily_loss_recovery_pct", 1.5), "daily_loss_recovery_pct"
            ),
            daily_loss_flatten_enabled=bool(_get(risk, "daily_loss_flatten_enabled", True)),
            daily_loss_flatten_pct=_finite(
                _get(risk, "daily_loss_flatten_pct", 5.0), "daily_loss_flatten_pct"
            ),
            portfolio_heat_block_pct=_finite(
                _get(risk, "portfolio_heat_block_pct", 2.0), "portfolio_heat_block_pct"
            ),
            margin_guardian_enabled=bool(_get(risk, "margin_guardian_enabled", True)),
            margin_warn_level_pct=_finite(_get(risk, "margin_warn_pct", 200.0), "margin_warn_pct"),
            margin_block_entry_level_pct=_finite(
                _get(risk, "margin_block_entry_pct", 150.0), "margin_block_entry_pct"
            ),
            margin_flatten_level_pct=_finite(
                _get(risk, "margin_flatten_pct", 100.0), "margin_flatten_pct"
            ),
            graded_exposure_enabled=bool(_get(governor, "graded_exposure", True)),
            cooldown_hours=_finite(_get(risk_management, "cooldown_hours", 4.0), "cooldown_hours"),
            cooldown_sizing_factor=_finite(
                _get(risk_management, "cooldown_sizing_factor", 0.50),
                "cooldown_sizing_factor",
            ),
            max_correlated_exposure_factor=_finite(
                _get(risk_management, "max_correlated_exposure_factor", 1.50),
                "max_correlated_exposure_factor",
            ),
            correlation_threshold=_finite(
                _get(risk_management, "correlation_threshold", 0.70),
                "correlation_threshold",
            ),
        )

    def validate(self) -> None:
        pct_fields = (
            "risk_per_trade_pct", "max_risk_pct_per_trade", "max_total_risk_pct",
            "daily_loss_limit_pct", "weekly_drawdown_limit_pct",
            "hard_stop_drawdown_pct", "daily_loss_recovery_pct",
            "daily_loss_flatten_pct", "portfolio_heat_block_pct",
            "margin_warn_level_pct", "margin_block_entry_level_pct",
            "margin_flatten_level_pct", "max_directional_exposure_pct",
            "max_regime_exposure_pct",
        )
        for name in pct_fields:
            value = _finite(getattr(self, name), name)
            if value < 0.0 or value > 100.0:
                raise ValueError(f"{name} must be in [0, 100]")
            setattr(self, name, value)

        if self.risk_per_trade_pct <= 0.0:
            raise ValueError("risk_per_trade_pct must be > 0")
        if self.max_risk_pct_per_trade < self.risk_per_trade_pct:
            raise ValueError("max_risk_pct_per_trade must be >= risk_per_trade_pct")
        if self.max_total_risk_pct < self.max_risk_pct_per_trade:
            raise ValueError("max_total_risk_pct must be >= max_risk_pct_per_trade")
        if not (0.0 < self.daily_loss_limit_pct <= self.weekly_drawdown_limit_pct
                <= self.hard_stop_drawdown_pct):
            raise ValueError("drawdown limits must satisfy daily <= weekly <= hard stop")
        if not (0.0 <= self.daily_loss_recovery_pct <= self.daily_loss_limit_pct):
            raise ValueError("daily_loss_recovery_pct must be within the daily loss limit")
        if self.daily_loss_flatten_pct <= 0.0 or self.portfolio_heat_block_pct <= 0.0:
            raise ValueError("flatten and portfolio heat limits must be > 0")
        if not (0.0 < self.margin_flatten_level_pct
                <= self.margin_block_entry_level_pct
                <= self.margin_warn_level_pct):
            raise ValueError("margin levels must satisfy flatten <= block <= warn")

        for name in (
            "max_open_positions", "max_positions_per_symbol",
            "max_correlated_positions", "max_cluster_same_direction",
            "max_currency_exposure", "max_sector_exposure",
            "max_positions_per_timeframe_class",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be an integer >= 1")
        if self.max_positions_per_symbol > self.max_open_positions:
            raise ValueError("max_positions_per_symbol cannot exceed max_open_positions")
        if self.max_correlated_positions > self.max_open_positions:
            raise ValueError("max_correlated_positions cannot exceed max_open_positions")
        if isinstance(self.max_broker_positions, bool) or int(self.max_broker_positions) < 0:
            raise ValueError("max_broker_positions must be an integer >= 0")
        self.max_broker_positions = int(self.max_broker_positions)

        for name in (
            "allow_intentional_hedge", "daily_loss_flatten_enabled",
            "margin_guardian_enabled", "graded_exposure_enabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")

        for name in ("max_campaign_exposure_units", "cooldown_hours"):
            value = _finite(getattr(self, name), name)
            if value < 0.0:
                raise ValueError(f"{name} must be >= 0")
            setattr(self, name, value)
        for name in (
            "max_campaign_exposure_fraction", "hedge_ratio_cap",
            "cooldown_sizing_factor", "correlation_threshold",
        ):
            value = _finite(getattr(self, name), name)
            if value < 0.0 or value > 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
            setattr(self, name, value)
        for name in ("max_correlated_exposure_factor",):
            value = _finite(getattr(self, name), name)
            if value < 1.0:
                raise ValueError(f"{name} must be >= 1")
            setattr(self, name, value)

    def apply_updates(self, updates: Mapping[str, Any]) -> None:
        """Validate the complete candidate before atomically mutating this object."""
        allowed = {item.name for item in fields(self)}
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"unknown owner limit(s): {', '.join(sorted(unknown))}")
        candidate = replace(self, **dict(updates))
        candidate.validate()
        for item in fields(self):
            setattr(self, item.name, getattr(candidate, item.name))

    def to_dict(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}
