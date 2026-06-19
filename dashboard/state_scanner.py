"""APEX TRADER — Dashboard Scanner Results Mixin."""

from typing import Any

from config import INSTRUMENT_REGISTRY, get_instrument
from dashboard.state_helpers import (
    HelpersMixin,
    base_factor_set,
    normalize_direction,
    safe_float,
    value,
)


class ScannerMixin(HelpersMixin):
    """get_scanner_results()."""

    def get_scanner_results(self) -> dict:
        if not self.is_live:
            return {
                "instruments": [],
                "ready_count": 0,
                "watchlist_count": 0,
                "total_count": 0,
            }

        ed = getattr(self, "_event_driven_system", None)
        if ed is not None:
            return self._ed_scanner_results()

        scanner = getattr(self._trading_loop, "scanner", None)
        report = getattr(scanner, "last_report", None) if scanner else None

        results_raw: list[Any] = []
        ready_count = 0
        watchlist_count = 0
        total_count = 0

        if report is not None:
            if hasattr(report, "results"):
                results_raw = list(getattr(report, "results", []))
                ready_count = int(getattr(report, "ready_count", 0))
                watchlist_count = int(getattr(report, "watchlist_count", 0))
                total_count = int(
                    getattr(report, "total_pairs_scanned", len(results_raw))
                )
            elif isinstance(report, list):
                results_raw = list(report)

        instruments: list[dict[str, Any]] = []
        for result in results_raw:
            symbol = str(value(result, "pair", "symbol", default="")).upper()
            if not symbol:
                continue

            try:
                info = get_instrument(symbol)
                name = info.name
                category = info.category.value
            except Exception:
                name = symbol
                category = str(
                    value(result, "instrument_category", "category", default="forex")
                ).lower()

            status = str(value(result, "status", default="WAITING")).upper()
            direction = normalize_direction(
                value(result, "direction", default="NEUTRAL")
            )

            instruments.append({
                "symbol": symbol,
                "name": name,
                "category": category,
                "direction": direction,
                "score": int(round(safe_float(value(result, "score", default=0), 0.0))),
                "status": status,
                "factors": self._scanner_factors(result),
            })

        if ready_count == 0 and watchlist_count == 0 and instruments:
            ready_count = sum(1 for i in instruments if i["status"] == "READY")
            watchlist_count = sum(1 for i in instruments if i["status"] == "WATCHLIST")

        instruments.sort(key=lambda i: i["score"], reverse=True)

        if not instruments:
            enabled_symbols: list[str] = []
            try:
                enabled_symbols = list(getattr(self._trading_loop.config, "enabled_pairs", []))
            except Exception:
                enabled_symbols = list(INSTRUMENT_REGISTRY.keys())

            for symbol in enabled_symbols:
                try:
                    info = get_instrument(symbol)
                    name = info.name
                    category = info.category.value
                except Exception:
                    name = symbol
                    category = "forex"
                instruments.append({
                    "symbol": symbol,
                    "name": name,
                    "category": category,
                    "direction": "NEUTRAL",
                    "score": 0,
                    "status": "WAITING",
                    "factors": base_factor_set(),
                })

        if total_count == 0:
            total_count = len(instruments)

        return {
            "instruments": instruments,
            "ready_count": ready_count,
            "watchlist_count": watchlist_count,
            "total_count": total_count,
        }

    def _ed_scanner_results(self) -> dict:
        """Scanner results from WorldModelStore in event-driven mode."""
        ed = self._event_driven_system
        wm_store = getattr(ed, "world_model_store", None)
        if wm_store is None:
            return {"instruments": [], "ready_count": 0, "watchlist_count": 0, "total_count": 0}

        instruments: list[dict[str, Any]] = []
        ready_count = 0
        watchlist_count = 0

        for symbol in INSTRUMENT_REGISTRY:
            wm = wm_store.get(symbol)
            try:
                info = get_instrument(symbol)
                name = info.name
                category = info.category.value
            except Exception:
                name = symbol
                category = "forex"

            if wm is None:
                instruments.append({
                    "symbol": symbol,
                    "name": name,
                    "category": category,
                    "direction": "NEUTRAL",
                    "score": 0,
                    "status": "WAITING",
                    "factors": base_factor_set(),
                })
                continue

            zones = getattr(wm, "entry_zones", [])
            has_zones = bool(zones)
            direction = "NEUTRAL"
            score = 0

            if has_zones:
                best = zones[0]
                direction = normalize_direction(getattr(best, "direction", "NEUTRAL"))
                score = int(getattr(best, "conviction", 0) or 0)

            status = "READY" if has_zones else "WAITING"
            if has_zones:
                ready_count += 1

            factors = base_factor_set()
            structure = getattr(wm, "structure", {})
            if structure:
                factors["structure"] = 1
            fvgs = getattr(wm, "fvgs", {})
            if fvgs:
                factors["fvg"] = 1
            obs = getattr(wm, "order_blocks", {})
            if obs:
                factors["ob"] = 1
            liq = getattr(wm, "liquidity", {})
            if liq:
                factors["liquidity"] = 1

            instruments.append({
                "symbol": symbol,
                "name": name,
                "category": category,
                "direction": direction,
                "score": score,
                "status": status,
                "factors": factors,
            })

        instruments.sort(key=lambda i: i["score"], reverse=True)
        return {
            "instruments": instruments,
            "ready_count": ready_count,
            "watchlist_count": watchlist_count,
            "total_count": len(instruments),
        }
