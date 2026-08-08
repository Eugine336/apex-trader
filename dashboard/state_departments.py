"""APEX TRADER — Department Summary (9-department organisation).

Aggregates a single at-a-glance health + headline summary for each of the nine
departments of the trading organism, reusing the live data the other mixins
already expose.  This is the data behind the dashboard home (org chart) and the
per-department cards.

It collects **no new data** — every value is derived from subsystems that are
already live (WorldModel votes, ComplianceDivision subsystems, PortfolioGovernor
exposure, GovernanceDivision status, the RecommendationGateway, broker health,
open positions, etc.).  Each department is wrapped in its own try/except so a
fault in one never blanks the whole board.

Department status vocabulary:
  * ``active``   — wired and doing its job on the live path
  * ``idle``     — wired but nothing attached yet (system not live)
  * ``degraded`` — wired but reporting a problem (e.g. broker down, frozen)
  * ``disabled`` — feature exists but is switched off by config
  * ``error``    — failed to read this department's state
"""

from __future__ import annotations

from typing import Any

# Canonical department order = the data-flow order of the organism.
SIGNAL_FLOW = [
    "intelligence",
    "consensus",
    "compliance",
    "portfolio",
    "execution",
    "operations",
    "learning",
    "governance",
    "command",
]


def _dept(
    key: str,
    number: int,
    name: str,
    mission: str,
    route: str,
    *,
    status: str = "idle",
    headline: str = "",
    metrics: list[dict] | None = None,
) -> dict:
    return {
        "key": key,
        "number": number,
        "name": name,
        "mission": mission,
        "route": route,
        "status": status,
        "headline": headline,
        "metrics": metrics or [],
    }


class DepartmentsMixin:
    """Provides ``get_departments()`` — the org-chart health summary."""

    def get_departments(self) -> dict:
        live = bool(getattr(self, "is_live", False))
        ctx = getattr(self, "_system_context", None)
        ed = getattr(self, "_event_driven_system", None)

        departments = [
            self._dept_intelligence(live),
            self._dept_consensus(live, ed),
            self._dept_compliance(live, ctx),
            self._dept_portfolio(live, ctx),
            self._dept_execution(live),
            self._dept_operations(live),
            self._dept_learning(live, ctx),
            self._dept_governance(live, ctx),
            self._dept_command(live),
        ]

        active = sum(1 for d in departments if d["status"] == "active")
        problems = [
            d["key"] for d in departments if d["status"] in ("degraded", "error")
        ]
        return {
            "source": "live" if live else "idle",
            "signal_flow": list(SIGNAL_FLOW),
            "departments": departments,
            "active_count": active,
            "department_count": len(departments),
            "problem_departments": problems,
        }

    # ── 1. Intelligence ───────────────────────────────────────────────────
    def _dept_intelligence(self, live: bool) -> dict:
        d = _dept(
            "intelligence", 1, "Intelligence",
            "Analysts produce structured market evidence — never trade, never veto.",
            "/module-votes",
        )
        if not live:
            d["headline"] = "Awaiting live system"
            return d
        try:
            votes = self.get_module_votes()
            pairs = votes.get("pairs", []) or []
            modules = votes.get("modules", []) or []
            participating = sum(
                1 for m in modules if (m.get("participation", 0) or 0) > 0
            )
            d["status"] = "active" if pairs else "idle"
            d["headline"] = (
                f"{len(modules)} analysts across {len(pairs)} instruments"
                if pairs else "No analysis cycles yet"
            )
            d["metrics"] = [
                {"label": "Analysts", "value": len(modules)},
                {"label": "Participating", "value": participating},
                {"label": "Instruments", "value": len(pairs)},
            ]
        except Exception as exc:  # noqa: BLE001
            d["status"] = "error"
            d["headline"] = f"read error: {exc}"
        return d

    # ── 2. Consensus ──────────────────────────────────────────────────────
    def _dept_consensus(self, live: bool, ed: Any) -> dict:
        d = _dept(
            "consensus", 2, "Consensus",
            "Transforms evidence into a market thesis — the active entry trigger.",
            "/ranker",
        )
        cfg = None
        try:
            cfg = getattr(getattr(ed, "_config", None), "consensus", None)
        except Exception:  # noqa: BLE001
            cfg = None
        try:
            trigger_on = bool(getattr(cfg, "active_trigger_enabled", False)) if cfg else False
            threshold = float(getattr(cfg, "conviction_threshold", 0.62)) if cfg else 0.62
            enabled = bool(getattr(cfg, "enabled", True)) if cfg else True
            if not live:
                d["status"] = "idle"
                d["headline"] = "Awaiting live system"
            elif not enabled:
                d["status"] = "disabled"
                d["headline"] = "Consensus engine disabled"
            else:
                d["status"] = "active"
                d["headline"] = (
                    "Market-driven trigger ARMED — zoneless entries enabled"
                    if trigger_on
                    else "Confirming zone entries (active trigger OFF)"
                )
            d["metrics"] = [
                {"label": "Active trigger", "value": "ON" if trigger_on else "OFF"},
                {"label": "Conviction ≥", "value": f"{threshold:.2f}"},
            ]
        except Exception as exc:  # noqa: BLE001
            d["status"] = "error"
            d["headline"] = f"read error: {exc}"
        return d

    # ── 3. Compliance ─────────────────────────────────────────────────────
    def _dept_compliance(self, live: bool, ctx: Any) -> dict:
        d = _dept(
            "compliance", 3, "Compliance",
            "Binary permit layer — the only necessary vetoes, fail-closed.",
            "/risk",
        )
        compliance = getattr(ctx, "compliance", None) if ctx else None
        if compliance is None:
            d["status"] = "idle" if live else "idle"
            d["headline"] = "Permit layer not wired"
            return d
        try:
            # Read the underlying permit sources for a live gate-status board.
            frozen = False
            dd = getattr(compliance, "_drawdown_guard", None)
            if dd is not None:
                try:
                    from brain.drawdown_guard import DrawdownMode

                    frozen = dd.get_status().mode == DrawdownMode.FROZEN.value
                except Exception:  # noqa: BLE001
                    frozen = False
            risk_state = "NORMAL"
            sm = getattr(compliance, "_portfolio_risk_sm", None)
            if sm is not None:
                risk_state = getattr(getattr(sm, "state", None), "name", "NORMAL")
            max_pos = int(getattr(compliance, "_max_open_positions", 0) or 0)
            entries_frozen = frozen or risk_state in (
                "DEFENSIVE", "REDUCING", "EMERGENCY"
            )
            d["status"] = "degraded" if entries_frozen else "active"
            d["headline"] = (
                "Entries FROZEN — " + ("drawdown FROZEN" if frozen else f"risk {risk_state}")
                if entries_frozen
                else "All permit gates clear"
            )
            d["metrics"] = [
                {"label": "Drawdown", "value": "FROZEN" if frozen else "OK"},
                {"label": "Risk state", "value": risk_state},
                {"label": "Max positions", "value": max_pos},
            ]
        except Exception as exc:  # noqa: BLE001
            d["status"] = "error"
            d["headline"] = f"read error: {exc}"
        return d

    # ── 4. Portfolio ──────────────────────────────────────────────────────
    def _dept_portfolio(self, live: bool, ctx: Any) -> dict:
        d = _dept(
            "portfolio", 4, "Portfolio",
            "Capital allocation, sizing and exposure across the whole book.",
            "/governor",
        )
        portfolio = getattr(ctx, "portfolio", None) if ctx else None
        if portfolio is None and not live:
            d["headline"] = "Awaiting live system"
            return d
        try:
            gov = self.get_governor()
            halted = bool(gov.get("trading_halted", False))
            open_pos = int(gov.get("open_positions", 0) or 0)
            max_pos = int(gov.get("max_open_positions", 0) or 0)
            daily_pct = float(gov.get("daily_pnl_pct", 0.0) or 0.0)
            cap_pct = float(gov.get("daily_loss_cap_pct", 0.0) or 0.0)
            ccy = gov.get("currency_exposure", {}) or {}
            wired = portfolio is not None
            if not live:
                d["status"] = "idle"
            elif halted:
                d["status"] = "degraded"
            else:
                d["status"] = "active" if wired else "idle"
            d["headline"] = (
                "Daily loss cap HALT — allocation frozen"
                if halted
                else f"{open_pos}/{max_pos} positions · {len(ccy)} currencies exposed"
            )
            d["metrics"] = [
                {"label": "Open / max", "value": f"{open_pos}/{max_pos}"},
                {"label": "Daily P&L", "value": f"{daily_pct:+.2f}%"},
                {"label": "Loss cap", "value": f"{cap_pct:.1f}%"},
            ]
        except Exception as exc:  # noqa: BLE001
            d["status"] = "error"
            d["headline"] = f"read error: {exc}"
        return d

    # ── 5. Execution ──────────────────────────────────────────────────────
    def _dept_execution(self, live: bool) -> dict:
        d = _dept(
            "execution", 5, "Execution",
            "The sole broker gateway — orders, modifies, closes, retries.",
            "/operations",
        )
        if not live:
            d["headline"] = "Awaiting live system"
            return d
        try:
            health = self.get_health()
            broker = health.get("broker", {}) or {}
            mt5 = bool(broker.get("mt5", False))
            deriv = bool(broker.get("deriv", False))
            any_conn = bool(broker.get("any_connected", mt5 or deriv))
            d["status"] = "active" if any_conn else "degraded"
            d["headline"] = (
                f"Brokers online — MT5 {'✓' if mt5 else '✗'} · Deriv {'✓' if deriv else '✗'}"
                if any_conn
                else "No broker connection"
            )
            d["metrics"] = [
                {"label": "MT5", "value": "ONLINE" if mt5 else "OFFLINE"},
                {"label": "Deriv", "value": "ONLINE" if deriv else "OFFLINE"},
            ]
        except Exception as exc:  # noqa: BLE001
            d["status"] = "error"
            d["headline"] = f"read error: {exc}"
        return d

    # ── 6. Operations ─────────────────────────────────────────────────────
    def _dept_operations(self, live: bool) -> dict:
        d = _dept(
            "operations", 6, "Operations",
            "Manages open positions — trailing, partials, exits, thesis re-validation.",
            "/position-health",
        )
        if not live:
            d["headline"] = "Awaiting live system"
            return d
        try:
            trades = self.get_open_trades()
            count = int(trades.get("count", 0) or 0)
            rows = trades.get("trades", []) or []
            net = round(sum(float(t.get("pnl_dollars", 0.0) or 0.0) for t in rows), 2)
            d["status"] = "active"
            d["headline"] = (
                f"Managing {count} open position{'s' if count != 1 else ''} · net ${net:+.2f}"
                if count
                else "No open positions"
            )
            d["metrics"] = [
                {"label": "Open", "value": count},
                {"label": "Net P&L", "value": f"${net:+.2f}"},
            ]
        except Exception as exc:  # noqa: BLE001
            d["status"] = "error"
            d["headline"] = f"read error: {exc}"
        return d

    # ── 7. Learning ───────────────────────────────────────────────────────
    def _dept_learning(self, live: bool, ctx: Any) -> dict:
        d = _dept(
            "learning", 7, "Learning",
            "Measures outcomes and recommends adaptations — never mutates directly.",
            "/learning",
        )
        if not live:
            d["headline"] = "Awaiting live system"
            return d
        try:
            gw = getattr(ctx, "recommendation_gateway", None) if ctx else None
            approved = rejected = 0
            if gw is not None:
                try:
                    s = gw.stats()
                    approved = int(s.get("approved", 0) or 0)
                    rejected = int(s.get("rejected", 0) or 0)
                except Exception:  # noqa: BLE001
                    pass
            mg = self.get_module_governor()
            counts = mg.get("counts", {}) or {}
            d["status"] = "active"
            d["headline"] = (
                f"{approved + rejected} recommendations measured "
                f"({approved} approved / {rejected} rejected)"
            )
            d["metrics"] = [
                {"label": "Recommendations", "value": approved + rejected},
                {"label": "Active modules", "value": int(counts.get("ACTIVE", 0) or 0)},
                {"label": "Shadowed", "value": int(counts.get("SHADOW", 0) or 0)},
            ]
        except Exception as exc:  # noqa: BLE001
            d["status"] = "error"
            d["headline"] = f"read error: {exc}"
        return d

    # ── 8. Governance ─────────────────────────────────────────────────────
    def _dept_governance(self, live: bool, ctx: Any) -> dict:
        d = _dept(
            "governance", 8, "Governance",
            "Authorises Learning's recommendations and contains runaway adaptation.",
            "/module-governor",
        )
        governance = getattr(ctx, "governance", None) if ctx else None
        if governance is None:
            d["status"] = "idle"
            d["headline"] = "Authoriser not wired (auto-approve)"
            return d
        try:
            st = governance.get_status()
            counts = st.get("decision_counts", {}) or {}
            approved = int(counts.get("AUTHORIZED", counts.get("approved", 0)) or 0)
            rejected = int(counts.get("REJECTED", counts.get("rejected", 0)) or 0)
            toxic = len(st.get("toxic_pairs", []) or [])
            d["status"] = "active"
            d["headline"] = (
                f"{approved} authorised / {rejected} rejected · {toxic} toxic pair(s)"
            )
            d["metrics"] = [
                {"label": "Authorised", "value": approved},
                {"label": "Rejected", "value": rejected},
                {"label": "Toxic pairs", "value": toxic},
            ]
        except Exception as exc:  # noqa: BLE001
            d["status"] = "error"
            d["headline"] = f"read error: {exc}"
        return d

    # ── 9. Command Center ─────────────────────────────────────────────────
    def _dept_command(self, live: bool) -> dict:
        d = _dept(
            "command", 9, "Command Center",
            "System health, uptime, configuration and control levers.",
            "/controls",
        )
        try:
            status = self.get_status()
            running = str(status.get("bot_status", "stopped")) == "running"
            uptime = float(status.get("uptime_seconds", 0.0) or 0.0)
            balance = float(status.get("account_balance", 0.0) or 0.0)
            d["status"] = "active" if (live and running) else "idle"
            d["headline"] = (
                f"LIVE · up {_fmt_uptime(uptime)} · balance ${balance:,.2f}"
                if running
                else "System stopped"
            )
            d["metrics"] = [
                {"label": "State", "value": "LIVE" if running else "STOPPED"},
                {"label": "Uptime", "value": _fmt_uptime(uptime)},
                {"label": "Balance", "value": f"${balance:,.2f}"},
            ]
        except Exception as exc:  # noqa: BLE001
            d["status"] = "error"
            d["headline"] = f"read error: {exc}"
        return d


def _fmt_uptime(seconds: float) -> str:
    seconds = int(max(0.0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"
