"""APEX TRADER — Dashboard Market Model Mixin (Phase 5).

Surfaces the dual WorldModel the Institutional Market Model upgrade introduced:

  * the **Confirmed** WorldModel — stable, non-repainting structure derived only
    from closed candles (``EventDrivenSystem._wm_store``), and
  * the **Developing** WorldModel — the live, forming-candle structure that
    evolves between candle closes (``EventDrivenSystem._developing_wm_store``).

For every tracked symbol the panel shows the two side-by-side: the probabilistic
bias from each store, the per-timeframe structure trend/confidence/event, and an
agreement flag so an operator can see at a glance when the developing read has
begun to diverge from the confirmed structure — the early-warning the
management-path advisory (Part A) acts on.

Read-only: this mixin never makes or mutates a decision.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

# Tactical timeframes compared for the agreement / divergence flag. Higher
# frames provide strategic context; these two are where developing structure
# most usefully diverges from confirmed ahead of a candle close.
_TACTICAL_TFS = ("M5", "H1")
_ALL_TFS = ("M5", "M15", "H1", "H4", "D1")


def _round(x: Any, ndigits: int = 4) -> float:
    try:
        return round(float(x), ndigits)
    except (TypeError, ValueError):
        return 0.0


def _trend_str(sa: Any) -> str:
    if sa is None:
        return "UNKNOWN"
    trend = getattr(sa, "trend", None)
    if trend is None:
        return "UNKNOWN"
    return trend.value if hasattr(trend, "value") else str(trend)


def _event_str(sa: Any) -> str:
    if sa is None:
        return "NONE"
    ev = getattr(sa, "last_event", None)
    if ev is None:
        return "NONE"
    return ev.value if hasattr(ev, "value") else str(ev)


class MarketModelMixin:
    """get_market_model() — confirmed vs developing structure per symbol."""

    def _confirmed_store(self) -> Any:
        ed = getattr(self, "_event_driven_system", None)
        return getattr(ed, "_wm_store", None) if ed is not None else None

    def _developing_store(self) -> Any:
        ed = getattr(self, "_event_driven_system", None)
        return getattr(ed, "_developing_wm_store", None) if ed is not None else None

    @staticmethod
    def _structure_direction(struct: Any) -> str:
        """Display direction derived from the freshest structural trend (V5).

        The WorldModel no longer stores a pre-computed directional bias; the panel
        reports a direction derived from the StructureAnalysis trend measurement
        (H1→H4→M5→M15→D1) at read time. This is a MEASUREMENT-derived display
        value, not a stored conclusion, and matches how the safety-floor scan
        direction is derived. Returns "LONG"/"SHORT"/"".
        """
        for tf in ("H1", "H4", "M5", "M15", "D1"):
            sa = struct.get(tf) if isinstance(struct, dict) else None
            if sa is None:
                continue
            trend = str(getattr(getattr(sa, "trend", None), "value", getattr(sa, "trend", "")) or "").upper()
            if "BULL" in trend:
                return "LONG"
            if "BEAR" in trend:
                return "SHORT"
            return ""
        return ""

    @staticmethod
    def _serialize_wm(wm: Any) -> dict:
        """Serialize a WorldModel's non-directional alignment + per-TF structure
        for the panel (Violation V5 — no stored directional bias)."""
        if wm is None:
            return {}
        try:
            alignment = wm.multi_tf_alignment_dict()
        except Exception:
            alignment = {}
        try:
            struct = wm.structure_by_tf()
        except Exception:
            struct = {}

        tf_rows = []
        for tf in _ALL_TFS:
            sa = struct.get(tf)
            if sa is None:
                continue
            tf_rows.append({
                "timeframe": tf,
                "trend": _trend_str(sa),
                "confidence": _round(getattr(sa, "confidence", 0.0)),
                "event": _event_str(sa),
            })

        return {
            # Direction is derived from the structural trend measurement at read
            # time (V5), never read from a stored directional bias.
            "bias_direction": MarketModelMixin._structure_direction(struct),
            "alignment_strength": _round(alignment.get("trend_strength", 0.0)),
            "alignment_degree": _round(alignment.get("alignment_degree", 0.0)),
            "conflict_score": _round(alignment.get("conflict_score", 0.0)),
            "strength": str(alignment.get("strength", "") or ""),
            "version": int(getattr(wm, "version", 0) or 0),
            "timestamp": (
                wm.timestamp.isoformat()
                if getattr(wm, "timestamp", None) is not None
                else ""
            ),
            "structure": tf_rows,
        }

    @staticmethod
    def _diverging_tfs(confirmed: dict, developing: dict) -> list[str]:
        """Tactical TFs where confirmed and developing trends disagree (both
        known and opposite — RANGING/UNKNOWN on either side is not a divergence).
        """
        conf_by_tf = {r["timeframe"]: r for r in confirmed.get("structure", [])}
        dev_by_tf = {r["timeframe"]: r for r in developing.get("structure", [])}
        out: list[str] = []
        for tf in _TACTICAL_TFS:
            c = conf_by_tf.get(tf, {}).get("trend", "UNKNOWN")
            d = dev_by_tf.get(tf, {}).get("trend", "UNKNOWN")
            if c in ("BULLISH", "BEARISH") and d in ("BULLISH", "BEARISH") and c != d:
                out.append(tf)
        return out

    @staticmethod
    def _agreement(confirmed: dict, developing: dict, diverging: list[str]) -> str:
        """One-word agreement flag for the panel's color coding."""
        if not developing:
            return "unknown"
        cdir = confirmed.get("bias_direction", "")
        ddir = developing.get("bias_direction", "")
        if diverging:
            return "diverge"
        if cdir and ddir and cdir in ("LONG", "SHORT") and ddir in ("LONG", "SHORT"):
            return "agree" if cdir == ddir else "diverge"
        return "partial"

    def get_market_model(self) -> dict:
        """Confirmed vs developing structure for every tracked symbol."""
        confirmed_store = self._confirmed_store()
        developing_store = self._developing_store()

        symbols: list[dict] = []
        confirmed_count = 0
        developing_count = 0
        diverging_count = 0

        if confirmed_store is not None:
            try:
                from config import INSTRUMENT_REGISTRY

                for sym in INSTRUMENT_REGISTRY:
                    try:
                        conf_wm = confirmed_store.get(sym)
                    except Exception:
                        conf_wm = None
                    dev_wm = None
                    if developing_store is not None:
                        try:
                            dev_wm = developing_store.get(sym)
                        except Exception:
                            dev_wm = None
                    if conf_wm is None and dev_wm is None:
                        continue

                    confirmed = self._serialize_wm(conf_wm)
                    developing = self._serialize_wm(dev_wm)
                    if confirmed:
                        confirmed_count += 1
                    if developing:
                        developing_count += 1
                    diverging = self._diverging_tfs(confirmed, developing)
                    agreement = self._agreement(confirmed, developing, diverging)
                    if agreement == "diverge":
                        diverging_count += 1

                    symbols.append({
                        "symbol": str(sym).upper(),
                        "confirmed": confirmed,
                        "developing": developing,
                        "agreement": agreement,
                        "diverging_timeframes": diverging,
                    })
            except Exception as exc:
                logger.debug("[state_market_model] read failed: {}", exc)

        # Diverging symbols first (the early-warning the operator cares about),
        # then by name for a stable order.
        symbols.sort(key=lambda s: (s["agreement"] != "diverge", s["symbol"]))

        return {
            "symbols": symbols,
            "symbol_count": len(symbols),
            "confirmed_count": confirmed_count,
            "developing_count": developing_count,
            "diverging_count": diverging_count,
            "developing_enabled": developing_store is not None,
            "source": "live" if self.is_live else "idle",
        }
