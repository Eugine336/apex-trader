"""
APEX TRADER — Risk Management Layer (L8)

The self-auditing organism optimises for expectancy but, beyond a per-trade
stop, has no explicit *loss containment*. L8 is the circuit-breaker layer: it
prevents catastrophic drawdowns, correlated-exposure blowups, and
overconcentration. It is the one new component that can *block* a trade — but it
only ever blocks on hard risk limits (never on signal quality), and every block
is logged loudly and persisted for the dashboard. Silent suppression is a bug.

Four cooperating pieces behind one gate:

* **DrawdownCircuitBreaker** — tracks the equity curve and trips graded states:
  ``NORMAL`` → ``COOLDOWN`` (rolling DD breach → halve sizing for N hours, then
  restore) → ``HALTED_DAILY`` (daily DD breach → no new trades until the next UTC
  day) → ``HALTED_HARD`` (hard DD breach → block everything + signal flatten).

* **CorrelationRiskMonitor** — maintains a periodically-recomputed returns
  correlation matrix and caps combined exposure across highly-correlated open
  positions, so "long EURUSD + long GBPUSD" can't quietly become one oversized
  directional bet.

* **ExposureManager** — hard caps on simultaneous positions, per-pair count,
  net-directional exposure, and per-regime concentration (integrates with L7).

* **RiskGate** — the single ``can_open_position`` entry point every trade passes
  through. Runs the checks in order, returns a structured ``(ok, reason)``, and
  logs + persists every denial.

Cold-start / disabled → everything passes and ``sizing_factor`` is 1.0, so the
system behaves *identically* to the pre-L8 pipeline until limits are configured
and breached. Pure standard library + math (no numpy / sklearn). Leaf module —
depends only on ``adaptive.tunable``.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from loguru import logger

from adaptive.tunable import TuningGuardMixin

# ── Defaults ─────────────────────────────────────────────────────────────────

from runtime_paths import data_dir as _data_dir  # noqa: E402

_DB_DIR = _data_dir()
_DB_PATH = _DB_DIR / "risk_management.db"

# Risk states (graded — each strictly more restrictive than the last).
RISK_NORMAL = "NORMAL"
RISK_COOLDOWN = "COOLDOWN"
RISK_HALTED_DAILY = "HALTED_DAILY"
RISK_HALTED_HARD = "HALTED_HARD"

_SECONDS_PER_DAY = 86400.0

_CREATE_EQUITY = """
CREATE TABLE IF NOT EXISTS equity_curve (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    equity  REAL NOT NULL DEFAULT 0.0,
    pnl     REAL NOT NULL DEFAULT 0.0,
    ts      REAL NOT NULL DEFAULT 0.0
)
"""

_CREATE_RISK_EVENTS = """
CREATE TABLE IF NOT EXISTS risk_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    pair    TEXT NOT NULL DEFAULT '',
    rule    TEXT NOT NULL DEFAULT '',
    reason  TEXT NOT NULL DEFAULT '',
    ts      REAL NOT NULL DEFAULT 0.0
)
"""

_CREATE_DRAWDOWN = """
CREATE TABLE IF NOT EXISTS drawdown_episodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    state       TEXT NOT NULL DEFAULT '',
    depth_pct   REAL NOT NULL DEFAULT 0.0,
    started_ts  REAL NOT NULL DEFAULT 0.0,
    ended_ts    REAL NOT NULL DEFAULT 0.0
)
"""

_CREATE_META = """
CREATE TABLE IF NOT EXISTS risk_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
)
"""


@dataclass
class RiskDecision:
    """The structured outcome of a gate check (one risk event when blocked)."""

    allowed: bool
    rule: str = ""
    reason: str = ""

    def as_tuple(self) -> Tuple[bool, str]:
        return self.allowed, self.reason


def _safe_pct(numer: float, denom: float) -> float:
    if denom <= 1e-9:
        return 0.0
    v = (numer / denom) * 100.0
    return v if math.isfinite(v) else 0.0


def _pearson(a: Sequence[float], b: Sequence[float]) -> float:
    """Pearson correlation of two equal-length return series (0 if degenerate)."""
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a = a[-n:]
    b = b[-n:]
    ma = sum(a) / n
    mb = sum(b) / n
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    va = sum((a[i] - ma) ** 2 for i in range(n))
    vb = sum((b[i] - mb) ** 2 for i in range(n))
    denom = math.sqrt(va * vb)
    if denom <= 1e-12:
        return 0.0
    r = cov / denom
    if not math.isfinite(r):
        return 0.0
    return max(-1.0, min(1.0, r))


def _returns(closes: Sequence[float]) -> List[float]:
    out: List[float] = []
    prev = None
    for c in closes or []:
        try:
            f = float(c)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(f):
            continue
        if prev is not None and prev != 0:
            out.append((f - prev) / prev)
        prev = f
    return out


class RiskManager(TuningGuardMixin):
    """Drawdown + correlation + exposure circuit breaker behind one gate."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        enabled: bool = True,
        daily_drawdown_limit_pct: float = 3.0,
        rolling_drawdown_limit_pct: float = 8.0,
        hard_stop_drawdown_pct: float = 15.0,
        cooldown_hours: float = 4.0,
        cooldown_sizing_factor: float = 0.5,
        max_simultaneous_positions: int = 10,
        max_per_pair_positions: int = 2,
        max_directional_exposure_pct: float = 60.0,
        correlation_threshold: float = 0.7,
        max_correlated_exposure_factor: float = 1.5,
        correlation_lookback_bars: int = 100,
        correlation_update_interval: int = 50,
        max_per_regime_pct: float = 40.0,
    ) -> None:
        self.enabled = bool(enabled)
        self._db_path = Path(db_path) if db_path is not None else _DB_PATH
        # Tunable knobs.
        self.daily_drawdown_limit_pct = float(daily_drawdown_limit_pct)
        self.rolling_drawdown_limit_pct = float(rolling_drawdown_limit_pct)
        self.hard_stop_drawdown_pct = float(hard_stop_drawdown_pct)
        self.cooldown_hours = float(cooldown_hours)
        self.cooldown_sizing_factor = float(cooldown_sizing_factor)
        self.max_simultaneous_positions = max(1, int(max_simultaneous_positions))
        self.max_per_pair_positions = max(1, int(max_per_pair_positions))
        self.max_directional_exposure_pct = float(max_directional_exposure_pct)
        self.correlation_threshold = float(correlation_threshold)
        self.max_correlated_exposure_factor = float(max_correlated_exposure_factor)
        self.correlation_lookback_bars = max(3, int(correlation_lookback_bars))
        self.correlation_update_interval = max(1, int(correlation_update_interval))
        self.max_per_regime_pct = float(max_per_regime_pct)

        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

        # Equity-tracking runtime state.
        self._peak_equity: float = 0.0
        self._current_equity: float = 0.0
        self._day_start_equity: float = 0.0
        self._day_index: float = 0.0  # floor(ts / day) of the active day
        self._state: str = RISK_NORMAL
        self._cooldown_until: float = 0.0
        self._episode_start_ts: float = 0.0
        self._closes_since_corr: int = 0
        # pair -> {pair: corr}; symmetric, diagonal omitted.
        self._corr_matrix: Dict[str, Dict[str, float]] = {}

        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[RiskManager] could not create db dir: {}", exc)
        self._connect()
        self._load_state()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def _connect(self) -> None:
        try:
            self._conn = sqlite3.connect(
                str(self._db_path), timeout=10, check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(_CREATE_EQUITY)
            self._conn.execute(_CREATE_RISK_EVENTS)
            self._conn.execute(_CREATE_DRAWDOWN)
            self._conn.execute(_CREATE_META)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[RiskManager] DB connect/init failed ({}): {}", self._db_path, exc)
            self._conn = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    logger.debug("[RiskManager] conn.close() failed during cleanup")
                self._conn = None

    def _load_state(self) -> None:
        if self._conn is None:
            return
        with self._lock:
            try:
                def _meta(key: str, default: float) -> float:
                    row = self._conn.execute(
                        "SELECT value FROM risk_meta WHERE key=?", (key,)
                    ).fetchone()
                    return float(row[0]) if row and row[0] else default

                self._peak_equity = _meta("peak_equity", 0.0)
                self._day_start_equity = _meta("day_start_equity", 0.0)
                self._day_index = _meta("day_index", 0.0)
                self._cooldown_until = _meta("cooldown_until", 0.0)
                row = self._conn.execute(
                    "SELECT equity FROM equity_curve ORDER BY ts DESC, id DESC LIMIT 1"
                ).fetchone()
                self._current_equity = float(row[0]) if row and row[0] else 0.0
            except Exception as exc:  # noqa: BLE001
                logger.debug("[RiskManager] _load_state failed: {}", exc)

    def _set_meta(self, key: str, value) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO risk_meta (key, value) VALUES (?, ?)",
                (str(key), str(value)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[RiskManager] _set_meta failed: {}", exc)

    # ── Equity / drawdown tracking (data ingestion — always allowed) ─────────────

    def on_trade_closed(self, pnl_dollars: float, account_balance: float) -> None:
        """Update the equity curve + drawdown state machine on a closed trade.

        ``account_balance`` is the post-close balance (authoritative equity
        anchor). Pure ingestion — never blocked by the TunerAgent. Recomputes
        peak/daily drawdown and transitions the breaker state with logging.
        """
        if not self.enabled:
            return
        try:
            pnl = float(pnl_dollars)
            equity = float(account_balance)
        except (TypeError, ValueError):
            return
        if not (math.isfinite(pnl) and math.isfinite(equity)):
            return
        now = time.time()
        with self._lock:
            self._roll_day(now, equity)
            self._current_equity = equity
            if equity > self._peak_equity:
                self._peak_equity = equity
            self._append_equity(equity, pnl, now)
            self._set_meta("peak_equity", self._peak_equity)
            self._evaluate_state(now)
            if self._conn is not None:
                try:
                    self._conn.commit()
                except Exception:  # noqa: BLE001
                    logger.debug("[RiskManager] commit after close failed")

    def _roll_day(self, now: float, equity: float) -> None:
        day = math.floor(now / _SECONDS_PER_DAY)
        if day != self._day_index:
            self._day_index = day
            self._day_start_equity = equity if equity > 0 else self._current_equity
            self._set_meta("day_index", self._day_index)
            self._set_meta("day_start_equity", self._day_start_equity)
            # A new day clears the daily halt (rolling/hard states persist until DD recovers).
            if self._state == RISK_HALTED_DAILY:
                self._transition(RISK_NORMAL, now, reason="new UTC day — daily halt cleared")
        if self._day_start_equity <= 0 and equity > 0:
            self._day_start_equity = equity
            self._set_meta("day_start_equity", self._day_start_equity)

    def _append_equity(self, equity: float, pnl: float, ts: float) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT INTO equity_curve (equity, pnl, ts) VALUES (?,?,?)",
                (equity, pnl, ts),
            )
            self._conn.execute(
                """DELETE FROM equity_curve WHERE id NOT IN (
                       SELECT id FROM equity_curve ORDER BY ts DESC, id DESC LIMIT 5000
                   )"""
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[RiskManager] _append_equity failed: {}", exc)

    def rolling_drawdown_pct(self) -> float:
        """Peak-to-current drawdown as a positive percentage."""
        if self._peak_equity <= 0:
            return 0.0
        return max(0.0, _safe_pct(self._peak_equity - self._current_equity, self._peak_equity))

    def daily_drawdown_pct(self) -> float:
        """Day-start-to-current drawdown as a positive percentage."""
        if self._day_start_equity <= 0:
            return 0.0
        return max(0.0, _safe_pct(self._day_start_equity - self._current_equity, self._day_start_equity))

    def _evaluate_state(self, now: float) -> None:
        """Recompute the breaker state from current drawdowns (with hysteresis)."""
        rolling = self.rolling_drawdown_pct()
        daily = self.daily_drawdown_pct()

        # Hard stop is absorbing — once tripped, only a fresh equity high clears it.
        if rolling >= self.hard_stop_drawdown_pct:
            if self._state != RISK_HALTED_HARD:
                self._transition(RISK_HALTED_HARD, now,
                                 reason=f"rolling DD {rolling:.1f}% >= hard {self.hard_stop_drawdown_pct:.1f}%")
            return
        if self._state == RISK_HALTED_HARD:
            # Stay halted until drawdown recovers well under the rolling limit.
            if rolling <= self.rolling_drawdown_limit_pct * 0.5:
                self._transition(RISK_NORMAL, now, reason=f"recovered (DD {rolling:.1f}%)")
            else:
                return

        # Daily halt: blocks new trades for the rest of the UTC day.
        if daily >= self.daily_drawdown_limit_pct:
            if self._state != RISK_HALTED_DAILY:
                self._transition(RISK_HALTED_DAILY, now,
                                 reason=f"daily DD {daily:.1f}% >= {self.daily_drawdown_limit_pct:.1f}%")
            return

        # Rolling cooldown: reduce sizing for cooldown_hours.
        if rolling >= self.rolling_drawdown_limit_pct:
            self._cooldown_until = now + self.cooldown_hours * 3600.0
            self._set_meta("cooldown_until", self._cooldown_until)
            if self._state != RISK_COOLDOWN:
                self._transition(RISK_COOLDOWN, now,
                                 reason=f"rolling DD {rolling:.1f}% >= {self.rolling_drawdown_limit_pct:.1f}%")
            return

        # Cooldown timer expiry → restore.
        if self._state == RISK_COOLDOWN:
            if now >= self._cooldown_until:
                self._transition(RISK_NORMAL, now, reason="cooldown elapsed")
            return

        if self._state != RISK_NORMAL and self._state != RISK_HALTED_HARD:
            self._transition(RISK_NORMAL, now, reason="risk normalised")

    def _transition(self, new_state: str, now: float, *, reason: str) -> None:
        old = self._state
        if old == new_state:
            return
        self._state = new_state
        # Close prior episode / open a new one for non-normal states.
        if self._conn is not None:
            try:
                if old != RISK_NORMAL and self._episode_start_ts > 0:
                    self._conn.execute(
                        "UPDATE drawdown_episodes SET ended_ts=? WHERE ended_ts=0 AND state=?",
                        (now, old),
                    )
                if new_state != RISK_NORMAL:
                    depth = max(self.rolling_drawdown_pct(), self.daily_drawdown_pct())
                    self._conn.execute(
                        "INSERT INTO drawdown_episodes (state, depth_pct, started_ts, ended_ts)"
                        " VALUES (?,?,?,0)",
                        (new_state, depth, now),
                    )
                    self._episode_start_ts = now
            except Exception as exc:  # noqa: BLE001
                logger.debug("[RiskManager] episode bookkeeping failed: {}", exc)
        level = logger.warning if new_state in (RISK_HALTED_DAILY, RISK_HALTED_HARD) else logger.info
        level("[RiskManager] state {} → {} ({})", old, new_state, reason)

    # ── Correlation matrix (data ingestion) ──────────────────────────────────────

    def update_correlations(self, closes_by_pair: Dict[str, Sequence[float]]) -> None:
        """Recompute the pairwise returns-correlation matrix from recent closes.

        Called periodically (not every tick). ``closes_by_pair`` maps pair →
        recent close list. No-op when disabled.
        """
        if not self.enabled or not closes_by_pair:
            return
        with self._lock:
            self._closes_since_corr += 1
            if self._closes_since_corr < self.correlation_update_interval:
                # Cheap throttle so callers can fire it freely.
                if self._corr_matrix:
                    return
            self._closes_since_corr = 0
            returns: Dict[str, List[float]] = {}
            for pair, closes in closes_by_pair.items():
                rs = _returns(list(closes)[-self.correlation_lookback_bars:])
                if len(rs) >= 3:
                    returns[str(pair)] = rs
            matrix: Dict[str, Dict[str, float]] = {}
            pairs = list(returns.keys())
            for i, p in enumerate(pairs):
                for q in pairs[i + 1:]:
                    r = _pearson(returns[p], returns[q])
                    matrix.setdefault(p, {})[q] = r
                    matrix.setdefault(q, {})[p] = r
            self._corr_matrix = matrix

    def correlation(self, a: str, b: str) -> float:
        if a == b:
            return 1.0
        return float(self._corr_matrix.get(str(a), {}).get(str(b), 0.0))

    # ── The gate (the one place that can block) ──────────────────────────────────

    def can_open_position(
        self,
        pair: str,
        direction: str,
        *,
        open_positions: Sequence[dict],
        account_balance: float = 0.0,
        regime: Optional[str] = None,
    ) -> RiskDecision:
        """Decide whether a new position may open. Checks run hard → soft.

        ``open_positions`` is a list of dicts with at least ``pair`` and
        ``direction`` (optionally ``regime``). Returns a :class:`RiskDecision`;
        every denial is logged loudly and persisted. Disabled / cold → always
        allowed (true no-op).
        """
        if not self.enabled:
            return RiskDecision(True, reason="risk management off")

        pair = str(pair or "")
        direction = str(direction or "").upper()
        positions = list(open_positions or [])

        # 1) Drawdown breaker (hardest).
        if self._state == RISK_HALTED_HARD:
            return self._deny(pair, "hard_stop",
                              f"HARD STOP active (rolling DD {self.rolling_drawdown_pct():.1f}%)")
        if self._state == RISK_HALTED_DAILY:
            return self._deny(pair, "daily_halt",
                              f"daily drawdown halt (DD {self.daily_drawdown_pct():.1f}%)")

        # 2) Exposure caps.
        if len(positions) >= self.max_simultaneous_positions:
            return self._deny(pair, "max_positions",
                              f"{len(positions)} open >= max {self.max_simultaneous_positions}")
        same_pair = sum(1 for p in positions if str(p.get("pair", "")) == pair)
        if same_pair >= self.max_per_pair_positions:
            return self._deny(pair, "max_per_pair",
                              f"{same_pair} on {pair} >= max {self.max_per_pair_positions}")

        # 3) Net-directional exposure (count-based proxy, % of the position book).
        total_after = len(positions) + 1
        same_dir_after = sum(1 for p in positions if str(p.get("direction", "")).upper() == direction) + 1
        dir_pct = _safe_pct(same_dir_after, total_after)
        if total_after >= 3 and dir_pct > self.max_directional_exposure_pct:
            return self._deny(pair, "directional_exposure",
                              f"{direction} exposure {dir_pct:.0f}% > max {self.max_directional_exposure_pct:.0f}%")

        # 4) Correlated exposure: count same-direction highly-correlated peers.
        corr_peers = 0
        for p in positions:
            other = str(p.get("pair", ""))
            if not other or other == pair:
                continue
            if str(p.get("direction", "")).upper() != direction:
                continue
            if abs(self.correlation(pair, other)) >= self.correlation_threshold:
                corr_peers += 1
        # Allowed correlated cluster size scales with the exposure factor.
        max_cluster = max(1, int(round(self.max_correlated_exposure_factor)))
        if corr_peers >= max_cluster:
            return self._deny(pair, "correlated_exposure",
                              f"{corr_peers} correlated {direction} peer(s) >= cap {max_cluster}")

        # 5) Per-regime concentration (integrates with L7).
        if regime:
            reg = str(regime).upper()
            same_regime_after = sum(
                1 for p in positions if str(p.get("regime", "")).upper() == reg
            ) + 1
            reg_pct = _safe_pct(same_regime_after, total_after)
            if total_after >= 3 and reg_pct > self.max_per_regime_pct:
                return self._deny(pair, "regime_concentration",
                                  f"regime {reg} {reg_pct:.0f}% > max {self.max_per_regime_pct:.0f}%")

        return RiskDecision(True, reason="risk ok")

    def _deny(self, pair: str, rule: str, reason: str) -> RiskDecision:
        # Loud + persisted — a blocked trade must never vanish silently.
        logger.warning("[RiskManager] BLOCKED {} — {} ({})", pair or "?", rule, reason)
        if self._conn is not None:
            try:
                self._conn.execute(
                    "INSERT INTO risk_events (pair, rule, reason, ts) VALUES (?,?,?,?)",
                    (str(pair), str(rule), str(reason), time.time()),
                )
                self._conn.execute(
                    """DELETE FROM risk_events WHERE id NOT IN (
                           SELECT id FROM risk_events ORDER BY ts DESC, id DESC LIMIT 1000
                       )"""
                )
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[RiskManager] risk_event persist failed: {}", exc)
        return RiskDecision(False, rule=rule, reason=reason)

    # ── Consumption (read path) ──────────────────────────────────────────────────

    def sizing_factor(self) -> float:
        """De-risking multiplier in ``(0, 1]`` for the current breaker state.

        1.0 in NORMAL, ``cooldown_sizing_factor`` in COOLDOWN. (HALTED states
        block at the gate, so sizing there is moot but reported as the cooldown
        factor for safety.) Disabled → 1.0.
        """
        if not self.enabled:
            return 1.0
        if self._state == RISK_COOLDOWN:
            return max(0.01, min(1.0, self.cooldown_sizing_factor))
        if self._state in (RISK_HALTED_DAILY, RISK_HALTED_HARD):
            return max(0.01, min(1.0, self.cooldown_sizing_factor))
        return 1.0

    def should_flatten(self) -> bool:
        """True when the hard stop demands all positions be closed."""
        return self.enabled and self._state == RISK_HALTED_HARD

    @property
    def state(self) -> str:
        return self._state

    # ── State (dashboard / tunable) ─────────────────────────────────────────────

    def get_current_params(self) -> dict:
        return {
            "daily_drawdown_limit_pct": round(self.daily_drawdown_limit_pct, 4),
            "rolling_drawdown_limit_pct": round(self.rolling_drawdown_limit_pct, 4),
            "hard_stop_drawdown_pct": round(self.hard_stop_drawdown_pct, 4),
            "cooldown_hours": round(self.cooldown_hours, 4),
            "cooldown_sizing_factor": round(self.cooldown_sizing_factor, 4),
            "max_simultaneous_positions": int(self.max_simultaneous_positions),
            "max_per_pair_positions": int(self.max_per_pair_positions),
            "max_directional_exposure_pct": round(self.max_directional_exposure_pct, 4),
            "correlation_threshold": round(self.correlation_threshold, 4),
            "max_correlated_exposure_factor": round(self.max_correlated_exposure_factor, 4),
            "max_per_regime_pct": round(self.max_per_regime_pct, 4),
        }

    def apply_params(self, params: dict) -> None:
        if not params:
            return
        _floats = (
            "daily_drawdown_limit_pct", "rolling_drawdown_limit_pct",
            "hard_stop_drawdown_pct", "cooldown_hours", "cooldown_sizing_factor",
            "max_directional_exposure_pct", "correlation_threshold",
            "max_correlated_exposure_factor", "max_per_regime_pct",
        )
        for k in _floats:
            if k in params:
                setattr(self, k, float(params[k]))
        if "max_simultaneous_positions" in params:
            self.max_simultaneous_positions = max(1, int(params["max_simultaneous_positions"]))
        if "max_per_pair_positions" in params:
            self.max_per_pair_positions = max(1, int(params["max_per_pair_positions"]))

    def _recent_events(self, limit: int = 40) -> List[dict]:
        if self._conn is None:
            return []
        try:
            cur = self._conn.execute(
                "SELECT pair, rule, reason, ts FROM risk_events ORDER BY ts DESC LIMIT ?",
                (int(limit),),
            )
            return [
                {"pair": str(p), "rule": str(r), "reason": str(rs), "ts": float(ts)}
                for p, r, rs, ts in cur.fetchall()
            ]
        except Exception as exc:  # noqa: BLE001
            logger.debug("[RiskManager] _recent_events failed: {}", exc)
            return []

    def _equity_curve(self, limit: int = 200) -> List[dict]:
        if self._conn is None:
            return []
        try:
            cur = self._conn.execute(
                "SELECT equity, pnl, ts FROM equity_curve ORDER BY ts DESC, id DESC LIMIT ?",
                (int(limit),),
            )
            rows = [
                {"equity": round(float(e), 2), "pnl": round(float(p), 2), "ts": float(ts)}
                for e, p, ts in cur.fetchall()
            ]
            rows.reverse()
            return rows
        except Exception as exc:  # noqa: BLE001
            logger.debug("[RiskManager] _equity_curve failed: {}", exc)
            return []

    def _correlation_summary(self, limit: int = 30) -> List[dict]:
        out: List[dict] = []
        seen = set()
        for a, row in self._corr_matrix.items():
            for b, r in row.items():
                key = tuple(sorted((a, b)))
                if key in seen or abs(r) < self.correlation_threshold:
                    continue
                seen.add(key)
                out.append({"pair_a": a, "pair_b": b, "correlation": round(float(r), 4)})
        out.sort(key=lambda d: abs(d["correlation"]), reverse=True)
        return out[:limit]

    def get_state(self) -> dict:
        """Read-only snapshot for the dashboard."""
        return {
            "enabled": self.enabled,
            "source": "live",
            "state": self._state,
            "rolling_drawdown_pct": round(self.rolling_drawdown_pct(), 3),
            "daily_drawdown_pct": round(self.daily_drawdown_pct(), 3),
            "peak_equity": round(self._peak_equity, 2),
            "current_equity": round(self._current_equity, 2),
            "sizing_factor": round(self.sizing_factor(), 4),
            "should_flatten": self.should_flatten(),
            "cooldown_until": self._cooldown_until or None,
            "limits": {
                "daily_drawdown_limit_pct": round(self.daily_drawdown_limit_pct, 3),
                "rolling_drawdown_limit_pct": round(self.rolling_drawdown_limit_pct, 3),
                "hard_stop_drawdown_pct": round(self.hard_stop_drawdown_pct, 3),
                "max_simultaneous_positions": int(self.max_simultaneous_positions),
                "max_per_pair_positions": int(self.max_per_pair_positions),
                "max_directional_exposure_pct": round(self.max_directional_exposure_pct, 3),
                "correlation_threshold": round(self.correlation_threshold, 3),
                "max_per_regime_pct": round(self.max_per_regime_pct, 3),
            },
            "risk_events": self._recent_events(),
            "equity_curve": self._equity_curve(),
            "correlations": self._correlation_summary(),
        }


__all__ = [
    "RiskManager",
    "RiskDecision",
    "RISK_NORMAL",
    "RISK_COOLDOWN",
    "RISK_HALTED_DAILY",
    "RISK_HALTED_HARD",
]
