"""
APEX TRADER — Dashboard Brain Mixin (module observations + opportunity ranker)

Two of the system's richest output producers were invisible on the dashboard:

  * the **9 analysis modules** (momentum, order blocks, FVGs, liquidity, VWAP,
    structure, currency strength, wyckoff, volume) — each emits a directional
    observation per pair every scan, and
  * the **opportunity ranker** — which clusters those same observations into
    coherent, independently-scored trade ideas (``Opportunity``) by
    direction × horizon.

Both are produced by the event-driven analysis path (per-module observations and
ranked ``Opportunity`` candidates).  This mixin reads them from the event-driven
system — the same live source the Scanner panel uses — serialises the frozen
dataclasses, and adds the aggregates the panels need (per-module agreement,
horizon / direction / EV distributions).  It only reads; it never makes or
mutates a decision.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

# The directional observation/ranker subsystem (brain.opportunity_ranker) was
# retired with the Single-Reasoner cutover. These inert fallbacks keep the
# dashboard importable; the observation/ranker panels simply render empty now.
DEFAULT_SCALP_MODULES: tuple = ()
DEFAULT_SWING_MODULES: tuple = ()


def classify_timeframe(*args, **kwargs) -> str:
    return ""


def _round(x: Any, ndigits: int = 4) -> float:
    try:
        return round(float(x), ndigits)
    except (TypeError, ValueError):
        return 0.0


class BrainMixin:
    """get_module_votes() + get_ranker() — the brain's evidence + ranked ideas."""

    # ── Internal helpers ────────────────────────────────────────────────────
    def _scan_results(self) -> list[Any]:
        """The most recent per-pair scan results off the live loop, or []."""
        ctx = getattr(self, "_system_context", None)
        if ctx is not None:
            ed_sys = getattr(self, "_event_driven_system", None)
            if ed_sys is not None:
                wm_store = getattr(ed_sys, "_wm_store", None)
                if wm_store is not None:
                    try:
                        from config import INSTRUMENT_REGISTRY
                        results = []
                        for sym in INSTRUMENT_REGISTRY:
                            wm = wm_store.get(sym)
                            if wm is not None:
                                results.append(wm)
                        return results
                    except Exception:
                        pass
        return []

    def _horizon_modules(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Scalp / swing module classification (config override or defaults)."""
        ed = getattr(self, "_event_driven_system", None)
        rc = getattr(getattr(ed, "_config", None), "opportunity_ranker", None) if ed is not None else None
        scalp = tuple(getattr(rc, "scalp_modules", None) or DEFAULT_SCALP_MODULES)
        swing = tuple(getattr(rc, "swing_modules", None) or DEFAULT_SWING_MODULES)
        return scalp, swing

    def _ranker_execute(self) -> bool:
        ed = getattr(self, "_event_driven_system", None)
        rc = getattr(getattr(ed, "_config", None), "opportunity_ranker", None) if ed is not None else None
        return bool(getattr(rc, "execute", False))

    # ── Module observations ─────────────────────────────────────────────────
    def get_module_votes(self) -> dict:
        """Per-pair grid of the 9 modules' directional observations + per-module stats."""
        scalp, swing = self._horizon_modules()
        results = self._scan_results()

        pairs: list[dict] = []
        # module → {LONG, SHORT, NEUTRAL, conf_sum, conf_cnt, horizon}
        module_acc: dict[str, dict] = {}

        for result in results:
            pair = str(getattr(result, "pair", "") or "").upper()
            if not pair:
                continue
            observations = getattr(result, "votes", None) or []
            if not observations:
                continue

            evidence_rows: list[dict] = []
            for obs in observations:
                module = str(getattr(obs, "module", "") or "")
                if not module:
                    continue
                direction = str(getattr(obs, "direction", "NEUTRAL") or "NEUTRAL").upper()
                confidence = _round(getattr(obs, "confidence", 0.0))
                weight = _round(getattr(obs, "weight", 0.0), 3)
                signed = _round(getattr(obs, "signed", 0.0), 4)
                horizon = classify_timeframe(module, scalp, swing)
                evidence_rows.append({
                    "module": module,
                    "direction": direction,
                    "confidence": confidence,
                    "weight": weight,
                    "signed": signed,
                    "horizon": horizon,
                })

                acc = module_acc.setdefault(
                    module,
                    {"LONG": 0, "SHORT": 0, "NEUTRAL": 0, "conf_sum": 0.0,
                     "conf_cnt": 0, "horizon": horizon},
                )
                acc[direction if direction in acc else "NEUTRAL"] += 1
                if direction in ("LONG", "SHORT"):
                    acc["conf_sum"] += confidence
                    acc["conf_cnt"] += 1

            if not evidence_rows:
                continue

            long_n = sum(1 for r in vote_rows if r["direction"] == "LONG")
            short_n = sum(1 for r in vote_rows if r["direction"] == "SHORT")
            neutral_n = sum(1 for r in vote_rows if r["direction"] == "NEUTRAL")
            net = _round(sum(r["signed"] for r in vote_rows), 3)
            # V-027 — lead with the EVIDENCE SOURCES that informed this pair
            # (constitutional framing: the Brain reasons over evidence + decisions,
            # not directional vote tallies). The long/short/neutral counts below are
            # retained only as supplementary, backward-compatible operator context.
            evidence_sources = sorted({r["module"] for r in vote_rows if r["module"]})

            pairs.append({
                "pair": pair,
                "evidence_sources": evidence_sources,
                "evidence_source_count": len(evidence_sources),
                "direction": str(getattr(result, "direction", "NEUTRAL") or "NEUTRAL").upper(),
                "consensus_direction": str(
                    getattr(result, "consensus_direction", "") or ""
                ).upper(),
                "consensus_net": _round(getattr(result, "consensus_net", 0.0), 3),
                "consensus_agreement": _round(getattr(result, "consensus_agreement", 0.0), 3),
                "votes": vote_rows,
                # Supplementary / deprecated (V-027) — directional vote tallies kept
                # for the existing panel during the operator transition to the
                # evidence/decision framing above.
                "long_count": long_n,
                "short_count": short_n,
                "neutral_count": neutral_n,
                "net_signed": net,
            })

        pairs.sort(key=lambda p: abs(p["net_signed"]), reverse=True)

        modules = []
        for name, acc in module_acc.items():
            cnt = acc["conf_cnt"]
            modules.append({
                "module": name,
                "horizon": acc["horizon"],
                "long": acc["LONG"],
                "short": acc["SHORT"],
                "neutral": acc["NEUTRAL"],
                "participation": acc["LONG"] + acc["SHORT"],
                "avg_confidence": _round(acc["conf_sum"] / cnt, 3) if cnt else 0.0,
            })
        modules.sort(key=lambda m: m["participation"], reverse=True)

        return {
            "pairs": pairs,
            "modules": modules,
            "pair_count": len(pairs),
            "source": "live" if self.is_live else "idle",
        }

    # ── Opportunity ranker ──────────────────────────────────────────────────
    def get_ranker(self) -> dict:
        """Ranked opportunities (observation clusters) per pair + horizon/EV aggregates."""
        results = self._scan_results()
        execute = self._ranker_execute()

        pairs: list[dict] = []
        horizon_dist = {"SCALP": 0, "SWING": 0}
        direction_dist = {"LONG": 0, "SHORT": 0}
        ev_sum = 0.0
        ev_cnt = 0
        total_opps = 0
        override_count = 0

        for result in results:
            pair = str(getattr(result, "pair", "") or "").upper()
            if not pair:
                continue
            candidates = getattr(result, "candidates", None) or []
            if not candidates:
                continue

            opp_rows = [self._serialize_opportunity(o) for o in candidates]
            opp_rows = [o for o in opp_rows if o]
            if not opp_rows:
                continue

            for o in opp_rows:
                total_opps += 1
                horizon_dist[o["timeframe_class"]] = horizon_dist.get(o["timeframe_class"], 0) + 1
                direction_dist[o["direction"]] = direction_dist.get(o["direction"], 0) + 1
                ev_sum += o["expected_value"]
                ev_cnt += 1

            best = opp_rows[0]
            consensus_dir = str(getattr(result, "consensus_direction", "") or "").upper()
            live_dir = str(getattr(result, "direction", "") or "").upper()
            selected_horizon = str(getattr(result, "selected_horizon", "") or "")
            override = bool(
                execute
                and selected_horizon
                and consensus_dir
                and best["direction"] != consensus_dir
            )
            if override:
                override_count += 1

            pairs.append({
                "pair": pair,
                "best": best,
                "opportunities": opp_rows,
                "opportunity_count": len(opp_rows),
                "consensus_direction": consensus_dir,
                "live_direction": live_dir,
                "selected_horizon": selected_horizon,
                "ranker_override": override,
            })

        pairs.sort(key=lambda p: p["best"]["expected_value"], reverse=True)

        return {
            "pairs": pairs,
            "pair_count": len(pairs),
            "total_opportunities": total_opps,
            "execute": execute,
            "override_count": override_count,
            "horizon_distribution": horizon_dist,
            "direction_distribution": direction_dist,
            "avg_expected_value": _round(ev_sum / ev_cnt, 3) if ev_cnt else 0.0,
            "source": "live" if self.is_live else "idle",
        }

    def _serialize_opportunity(self, opp: Any) -> dict:
        try:
            contributors = list(getattr(opp, "contributors", []) or [])
            summary = getattr(opp, "summary", "")
            observations = list(getattr(opp, "votes", []) or [])
            return {
                "direction": str(getattr(opp, "direction", "") or "").upper(),
                "timeframe_class": str(getattr(opp, "timeframe_class", "") or "").upper(),
                "expected_value": _round(getattr(opp, "expected_value", 0.0), 3),
                "confidence": _round(getattr(opp, "confidence", 0.0), 3),
                "coherence": _round(getattr(opp, "coherence", 0.0), 3),
                "net_score": _round(getattr(opp, "net_score", 0.0), 3),
                "reward_risk": _round(getattr(opp, "reward_risk", 0.0), 2),
                "win_prob": _round(getattr(opp, "win_prob", 0.0), 3),
                "contributors": contributors,
                # Multi-opportunity provenance — the distinct real timeframes
                # behind this idea and how many observations formed it, so the panel
                # shows WHICH timeframes (not just the SCALP/SWING bucket) and a
                # stable candidate id where one is attached.
                "timeframes": [str(t).upper() for t in getattr(opp, "timeframes", []) or []],
                "vote_count": len(observations),
                "candidate_id": str(getattr(opp, "candidate_id", "") or ""),
                "summary": str(summary),
            }
        except Exception as exc:
            logger.debug("[state_brain] opportunity serialize failed: {}", exc)
            return {}
