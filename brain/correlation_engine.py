"""
APEX TRADER — Correlation Engine
Multiple open positions can look diversified while being the same bet.
This module enforces exposure discipline across currencies, asset clusters,
and directional concentration — covering forex, indices, commodities, crypto,
and synthetics.
"""

from dataclasses import dataclass
from typing import Sequence

from loguru import logger

from brain.currency_strength import CURRENCY_PAIRS


ASSET_CLUSTER: dict[str, str] = {
    "US100": "equity_risk_on", "US30": "equity_risk_on", "US500": "equity_risk_on",
    "GER40": "equity_risk_on", "UK100": "equity_risk_on", "JP225": "equity_risk_on",
    "AUS200": "equity_risk_on", "FRA40": "equity_risk_on", "ESP35": "equity_risk_on",
    "HK50": "equity_risk_on",
    "XAUUSD": "metals", "XAGUSD": "metals",
    "XBRUSD": "energy", "XTIUSD": "energy",
    "BTCUSD": "crypto", "ETHUSD": "crypto", "LTCUSD": "crypto",
    "XRPUSD": "crypto", "BNBUSD": "crypto", "SOLUSD": "crypto",
    "ADAUSD": "crypto", "DOTUSD": "crypto",
    "V10_1S": "volatility_index", "V25_1S": "volatility_index",
    "V50_1S": "volatility_index", "V75_1S": "volatility_index",
    "V100_1S": "volatility_index",
    "BOOM500": "boom", "BOOM1000": "boom",
    "CRASH500": "crash", "CRASH1000": "crash",
    "STPIDX": "step_index",
    "RNGBULL": "range_break", "RNGBEAR": "range_break",
    "JD10": "jump_index", "JD25": "jump_index", "JD50": "jump_index",
}


# Explicit intermarket relationships that pure currency decomposition misses.
# Gold (XAUUSD) is an anti-USD instrument, so it moves INVERSELY to the
# USD-strength proxies: when USDCHF / USDJPY rise (USD strong) Gold tends to
# fall, and vice-versa. Registering it here lets the consensus correlation
# vote read a Gold/DXY confirmation instead of abstaining.
INVERSE_CORRELATION: dict[str, tuple[str, ...]] = {
    "XAUUSD": ("USDCHF", "USDJPY"),
    "USDCHF": ("XAUUSD",),
    "USDJPY": ("XAUUSD",),
}


@dataclass
class OpenTrade:
    pair: str
    direction: str
    risk_pct: float


@dataclass
class ExposureMap:
    currency_exposures: dict[str, float]
    total_usd_exposure: float
    hedge_conflicts: list[str]
    max_single_currency_exposure: float
    is_safe: bool
    cluster_counts: dict[str, dict[str, int]] | None = None


class CorrelationEngine:
    """
    Controls aggregate currency exposure and asset-cluster concentration.
    Blocks hidden overexposure for ALL instrument types — forex via currency
    decomposition, non-forex via asset-cluster + directional caps.
    """

    def __init__(
        self,
        max_single_currency_exposure: float = 0.04,
        max_correlated_trades: int = 3,
        max_cluster_same_direction: int = 2,
        allow_intentional_hedge: bool = False,
    ):
        self.max_single_currency_exposure = max_single_currency_exposure
        self.max_correlated_trades = max_correlated_trades
        self.max_cluster_same_direction = max_cluster_same_direction
        self.allow_intentional_hedge = allow_intentional_hedge

    def calculate_exposure(self, open_trades: Sequence[OpenTrade | dict]) -> ExposureMap:
        exposures: dict[str, float] = {}
        normalized = [self._normalize_trade(t) for t in open_trades]

        for trade in normalized:
            base, quote = CURRENCY_PAIRS.get(trade.pair, (None, None))
            if not base or not quote:
                continue

            sign = 1.0 if trade.direction.upper() == "LONG" else -1.0
            exposures[base] = exposures.get(base, 0.0) + sign * trade.risk_pct
            exposures[quote] = exposures.get(quote, 0.0) - sign * trade.risk_pct

        hedge_conflicts = self._detect_hedge_conflicts(normalized)
        max_exposure = max((abs(v) for v in exposures.values()), default=0.0)

        cluster_counts = self._count_cluster_directions(normalized)

        is_safe = (
            max_exposure <= self.max_single_currency_exposure and not hedge_conflicts
        )

        return ExposureMap(
            currency_exposures={k: round(v, 4) for k, v in exposures.items()},
            total_usd_exposure=round(abs(exposures.get("USD", 0.0)), 4),
            hedge_conflicts=hedge_conflicts,
            max_single_currency_exposure=round(max_exposure, 4),
            is_safe=is_safe,
            cluster_counts=cluster_counts,
        )

    def can_open_trade(
        self,
        pair: str,
        direction: str,
        open_trades: Sequence[OpenTrade | dict],
        risk_pct: float = 0.02,
    ) -> tuple[bool, str]:
        candidate = OpenTrade(pair=pair, direction=direction, risk_pct=risk_pct)
        normalized = [self._normalize_trade(t) for t in open_trades] + [candidate]

        if self._count_correlated(pair, normalized[:-1]) >= self.max_correlated_trades:
            return False, f"Too many correlated trades with {pair}"

        cluster = ASSET_CLUSTER.get(pair.upper())
        if cluster:
            existing = [self._normalize_trade(t) for t in open_trades]
            same_dir = sum(
                1 for t in existing
                if ASSET_CLUSTER.get(t.pair) == cluster
                and t.direction.upper() == direction.upper()
            )
            if same_dir >= self.max_cluster_same_direction:
                return False, (
                    f"Cluster '{cluster}' already has {same_dir} {direction} "
                    f"trades (max {self.max_cluster_same_direction})"
                )

        exposure = self.calculate_exposure(normalized)
        if exposure.max_single_currency_exposure > self.max_single_currency_exposure:
            return False, (
                "Currency exposure limit exceeded "
                f"({exposure.max_single_currency_exposure:.2%} > {self.max_single_currency_exposure:.2%})"
            )
        if exposure.hedge_conflicts and not self.allow_intentional_hedge:
            return False, f"Hedge conflict detected: {exposure.hedge_conflicts[0]}"

        if exposure.hedge_conflicts and self.allow_intentional_hedge:
            logger.info("[hedge] intentional hedge permitted (bypassed conflict): {}", exposure.hedge_conflicts[0])

        return True, "Exposure profile is safe"

    def dxy_proxy(self, open_trades: list[OpenTrade | dict]) -> float:
        """
        Proxy for USD concentration from open positions.
        Positive means net long USD pressure, negative means net short.
        """
        normalized = [self._normalize_trade(t) for t in open_trades]
        exposure = self.calculate_exposure(normalized)
        usd_value = exposure.currency_exposures.get("USD", 0.0)
        return round(float(usd_value), 4)

    def inverse_correlates(self, pair: str) -> tuple[str, ...]:
        """Instruments registered as moving INVERSELY to ``pair``.

        Feeds the intermarket correlation vote — e.g. XAUUSD ↔ USDCHF/USDJPY
        (USD-strength proxies). Returns an empty tuple when no inverse
        relationship is registered.
        """
        return INVERSE_CORRELATION.get(pair.upper(), ())

    def _normalize_trade(self, trade: OpenTrade | dict) -> OpenTrade:
        if isinstance(trade, OpenTrade):
            return trade
        return OpenTrade(
            pair=str(trade.get("pair", "")).upper(),
            direction=str(trade.get("direction", "LONG")).upper(),
            risk_pct=float(trade.get("risk_pct", 0.02)),
        )

    def _count_correlated(self, pair: str, open_trades: list[OpenTrade]) -> int:
        if pair in CURRENCY_PAIRS:
            base, quote = CURRENCY_PAIRS[pair]
            count = 0
            for trade in open_trades:
                if trade.pair not in CURRENCY_PAIRS:
                    continue
                t_base, t_quote = CURRENCY_PAIRS[trade.pair]
                if quote in {t_base, t_quote} or base in {t_base, t_quote}:
                    count += 1
            return count

        cluster = ASSET_CLUSTER.get(pair.upper())
        if cluster:
            return sum(
                1 for t in open_trades
                if ASSET_CLUSTER.get(t.pair) == cluster
            )

        # Fallback for symbols in NEITHER the forex nor cluster maps (e.g. a
        # newly-listed crypto / index / synthetic). Never fail open: at minimum
        # cap stacking into the SAME exact instrument so concentration on an
        # unmapped symbol is still bounded by max_correlated_trades.
        pu = pair.upper()
        return sum(1 for t in open_trades if t.pair.upper() == pu)

    def _count_cluster_directions(
        self, trades: list[OpenTrade]
    ) -> dict[str, dict[str, int]]:
        """Count LONG/SHORT per asset cluster."""
        result: dict[str, dict[str, int]] = {}
        for t in trades:
            cluster = ASSET_CLUSTER.get(t.pair)
            if not cluster:
                continue
            if cluster not in result:
                result[cluster] = {"LONG": 0, "SHORT": 0}
            d = t.direction.upper()
            if d in result[cluster]:
                result[cluster][d] += 1
        return result

    def _detect_hedge_conflicts(self, open_trades: list[OpenTrade]) -> list[str]:
        conflicts: list[str] = []
        for i, first in enumerate(open_trades):
            if first.pair not in CURRENCY_PAIRS:
                continue
            first_base, first_quote = CURRENCY_PAIRS[first.pair]
            for second in open_trades[i + 1 :]:
                if second.pair not in CURRENCY_PAIRS:
                    continue
                second_base, second_quote = CURRENCY_PAIRS[second.pair]

                if (
                    first_quote == second_quote == "USD"
                    and first.direction != second.direction
                    and first_base != second_base
                ):
                    conflicts.append(
                        f"{first.direction} {first.pair} + {second.direction} {second.pair} "
                        f"creates synthetic cross exposure ({first_base}/{second_base})"
                    )

                if first_base == second_base and first.direction != second.direction:
                    conflicts.append(
                        f"Opposing positions on shared base currency: {first.pair} vs {second.pair}"
                    )
        return conflicts
