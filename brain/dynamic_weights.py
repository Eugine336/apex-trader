"""APEX TRADER — Dynamic Consensus Voting Weights.

The directional-consensus panel weighs each module's vote by a STATIC
``ConsensusConfig.weights`` entry — every module carries the same fixed weight
regardless of what the market is actually doing.  A static panel is a
*democratic noise system*: in a ranging chop the slow structural modules still
vote as loud as a clean trending day, and a volatility spike can let the fast
tactical modules dominate when they shouldn't (or vice versa).

This module makes the vote weight **contextual**.  The same base weights are
scaled per consensus cycle by three observable market-state factors:

* **Regime** — trending markets favour the higher-timeframe structural modules
  (strategic context); ranging markets favour the lower-timeframe tactical
  modules (they catch the turns earlier).
* **Volatility** — high volatility favours the fast LTF modules (they react
  first); low volatility favours the slow HTF modules (clean structure).
* **Recency** — a confirmed candle-close read is worth full weight; a live
  forming-bar estimate is discounted (still active, just less certain).

It is a pure, side-effect-free, dependency-light leaf module: no broker, no
network, no pandas/torch in the math.  It computes weights; it does not decide
direction (``Vote.signed`` is unchanged) and it does not learn (calibration is
a separate concern — this is the *mechanism*).
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from loguru import logger


# Regime label normalisation — accepts both the concept-module labels
# ("TREND"/"RANGE"/"VOLATILE"/"UNKNOWN") and the RegimeDetector labels
# ("TRENDING_UP"/"TRENDING_DOWN"/"RANGING"/"VOLATILE"/"QUIET"/"UNKNOWN") so the
# provider works no matter which regime source the caller feeds it.
_TRENDING = "trending"
_RANGING = "ranging"
_VOLATILE = "volatile"
_NEUTRAL = "neutral"


def _normalize_regime(regime: Any) -> str:
    """Map any regime spelling to one of trending/ranging/volatile/neutral."""
    s = str(regime or "").strip().upper()
    if not s:
        return _NEUTRAL
    if s.startswith("TREND"):  # TREND, TRENDING, TRENDING_UP, TRENDING_DOWN
        return _TRENDING
    if s.startswith("RANG"):  # RANGE, RANGING
        return _RANGING
    if s.startswith("VOLATIL"):  # VOLATILE
        return _VOLATILE
    # RANGE-equivalent quiet/contraction regimes lean tactical (LTF) too, but
    # we treat anything unrecognised (QUIET, UNKNOWN, "") as neutral so the
    # provider never invents a bias from a label it does not understand.
    return _NEUTRAL


class DynamicWeightProvider:
    """Computes context-dependent vote weights per consensus cycle.

    Replaces the static ``ConsensusConfig.weights`` lookup with weights that
    adapt to the current regime, volatility, and data recency.  Stateless and
    re-usable across symbols — call :meth:`compute_weights` once per consensus
    cycle with that cycle's market-state context.

    The provider never raises into the consensus path: any internal error
    transparently falls back to the unmodified base weights, so a malfunctioning
    weight computation can never block the panel.
    """

    def __init__(self, base_weights: Mapping[str, float], config: Any) -> None:
        # Snapshot the base (static) weights as the starting point.  Invalid /
        # non-finite entries are dropped so they cannot poison the math.
        self._base: dict[str, float] = {}
        for name, w in dict(base_weights or {}).items():
            try:
                wf = float(w)
            except (TypeError, ValueError):
                continue
            if math.isfinite(wf) and wf >= 0:
                self._base[str(name)] = wf
        self._config = config

    @property
    def base_weights(self) -> dict[str, float]:
        """The static base weights this provider scales (read-only copy)."""
        return dict(self._base)

    # ------------------------------------------------------------------
    # Scaling factors
    # ------------------------------------------------------------------

    def _regime_mults(self, regime: str) -> tuple[float, float]:
        """Return ``(htf_mult, ltf_mult)`` for the normalised regime."""
        cfg = self._config
        if regime == _TRENDING:
            return float(cfg.trending_htf_mult), float(cfg.trending_ltf_mult)
        if regime == _RANGING:
            return float(cfg.ranging_htf_mult), float(cfg.ranging_ltf_mult)
        if regime == _VOLATILE:
            return float(cfg.volatile_htf_mult), float(cfg.volatile_ltf_mult)
        return 1.0, 1.0  # neutral / unknown

    def _volatility_mults(self, volatility_ratio: float) -> tuple[float, float]:
        """Return ``(htf_mult, ltf_mult)`` for the current volatility ratio.

        ``volatility_ratio`` = current ATR / baseline ATR.  >1 means the market
        is more volatile than its own recent baseline.
        """
        cfg = self._config
        try:
            ratio = float(volatility_ratio)
        except (TypeError, ValueError):
            ratio = 1.0
        if not math.isfinite(ratio) or ratio <= 0:
            ratio = 1.0
        if ratio >= float(cfg.high_vol_threshold):
            return float(cfg.high_vol_htf_mult), float(cfg.high_vol_ltf_mult)
        if ratio <= float(cfg.low_vol_threshold):
            return float(cfg.low_vol_htf_mult), float(cfg.low_vol_ltf_mult)
        return 1.0, 1.0  # normal volatility

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_weights(
        self,
        symbol: str,
        regime: Any,
        volatility_ratio: float,
        is_confirmed: bool,
    ) -> dict[str, float]:
        """Return the adjusted per-module weights for this consensus cycle.

        Args:
            symbol: instrument the consensus is being computed for (logging only).
            regime: current market regime label (any supported spelling).
            volatility_ratio: current ATR / baseline ATR (>1 = elevated vol).
            is_confirmed: True for a confirmed candle-close read; False for a
                live forming-bar estimate (the latter is discounted).

        When the master switch is off, the base weights are returned unchanged.
        Any internal error also falls back to the unmodified base weights.
        """
        cfg = self._config
        base = dict(self._base)
        if cfg is None or not bool(getattr(cfg, "enabled", True)):
            return base
        if not base:
            return base
        try:
            htf = {str(m) for m in getattr(cfg, "htf_modules", []) or []}
            ltf = {str(m) for m in getattr(cfg, "ltf_modules", []) or []}

            regime_norm = _normalize_regime(regime)
            regime_htf, regime_ltf = self._regime_mults(regime_norm)
            vol_htf, vol_ltf = self._volatility_mults(volatility_ratio)
            # Recency applies uniformly to every module (HTF, LTF, unclassified).
            recency = 1.0 if is_confirmed else float(cfg.developing_weight_mult)

            lo = float(cfg.min_weight)
            hi = float(cfg.max_weight)

            out: dict[str, float] = {}
            for module, base_w in base.items():
                if module in htf:
                    regime_mult, vol_mult = regime_htf, vol_htf
                elif module in ltf:
                    regime_mult, vol_mult = regime_ltf, vol_ltf
                else:
                    # Unclassified module: no regime/volatility bias, recency only.
                    regime_mult, vol_mult = 1.0, 1.0
                w = base_w * regime_mult * vol_mult * recency
                if not math.isfinite(w):
                    w = base_w  # never emit a NaN/inf weight
                w = max(lo, min(hi, w))
                out[module] = w

            if logger is not None:
                logger.debug(
                    "[dynamic-weights] {} regime={} vol_ratio={:.2f} confirmed={} "
                    "regime_mult=(htf={:.2f},ltf={:.2f}) vol_mult=(htf={:.2f},ltf={:.2f}) "
                    "recency={:.2f}",
                    symbol,
                    regime_norm,
                    float(volatility_ratio) if _isfinite(volatility_ratio) else 1.0,
                    is_confirmed,
                    regime_htf, regime_ltf, vol_htf, vol_ltf, recency,
                )
            return out
        except Exception as exc:  # noqa: BLE001 — never break the consensus path
            logger.debug(
                "[dynamic-weights] {} weight computation failed ({}: {}) — "
                "falling back to static weights",
                symbol, type(exc).__name__, exc,
            )
            return base


def _isfinite(x: Any) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False
