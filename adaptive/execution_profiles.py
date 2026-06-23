"""
APEX TRADER — Execution Style Profiles (L5.5b)

L5.5a allocates capital across execution-style *fingerprints*. This layer
supplies the execution style itself. Instead of one fixed parameter set for
every trade, the system selects a named **Execution Profile** per trade — a
complete parameter vector describing HOW to act on a direction the rest of the
pipeline already chose:

    SL distance (ATR multiple) · TP R:R (TP1 / TP2) · trailing method +
    activation · partial-exit rules · entry confirmation · min-score · the
    conviction floor and a max-hold horizon.

Profiles are NOT hard-coded strategies. They are parameter vectors persisted in
SQLite that the rest of the evolution stack treats like any other tunable
object: the TunerAgent can adjust a profile's scalars, Parameter Evolution can
propose new vectors (= new profiles), the Capital Allocator scores each one via
the per-trade fingerprint (``entry_mode|horizon|profile``), and the Governor can
shadow/disable an underperforming profile. "Scalper", "Swing", "Breakout" stop
being permanent identities and become temporary, measurable hypotheses.

Selection is purely additive and de-risking. ``select_profile`` chooses a
profile from the trade's *context* (ranker horizon × regime × consensus
strength); the profile then overrides SL/TP/min-score in the entry engine and
BE/trailing/partial in the trade manager THROUGH THE SAME per-trade override
hooks the planner already uses. It never changes direction, never forces a
trade, never bypasses a hard risk veto, and a planner override always wins over
a profile default. With profiles disabled — or no match — every consumer falls
back to its config-level default, i.e. behaviour identical to the pre-profile
system (a true no-op).

Storage mirrors the proven ``capital_allocator`` / ``signal_ledger`` pattern:
sync ``sqlite3`` in WAL mode, exception-safe, survives restarts. Leaf module —
standard library + loguru only (plus the leaf ``adaptive.tunable`` guard mixin)
— so anything may depend on it without an import cycle.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from adaptive.tunable import TuningGuardMixin

# ── Defaults ─────────────────────────────────────────────────────────────────

from runtime_paths import data_dir as _data_dir  # noqa: E402

_DB_DIR = _data_dir()
_DB_PATH = _DB_DIR / "execution_profiles.db"

# Trailing methods a profile may request. ``breakeven_only`` keeps the
# break-even snap but disables structure trailing; the others trail (the trade
# manager only special-cases "none" to disable trailing entirely).
TRAILING_METHODS: frozenset[str] = frozenset(
    {"structure", "atr", "fixed_pips", "breakeven_only", "none"}
)

_CREATE_PROFILES = """
CREATE TABLE IF NOT EXISTS profiles (
    name                 TEXT PRIMARY KEY,
    sl_atr_multiplier    REAL NOT NULL,
    tp_rr_ratio          REAL NOT NULL,
    tp2_rr_ratio         REAL NOT NULL,
    max_hold_bars        INTEGER NOT NULL DEFAULT 0,
    trailing_method      TEXT NOT NULL DEFAULT 'structure',
    trailing_activation_r REAL NOT NULL DEFAULT 0.5,
    partial_exit_enabled INTEGER NOT NULL DEFAULT 1,
    partial_exit_r       REAL NOT NULL DEFAULT 1.0,
    partial_exit_pct     REAL NOT NULL DEFAULT 0.5,
    entry_confirmation   TEXT NOT NULL DEFAULT 'none',
    min_score_override   REAL NOT NULL DEFAULT 0.0,
    conviction_floor     REAL NOT NULL DEFAULT 0.0,
    builtin              INTEGER NOT NULL DEFAULT 0,
    active               INTEGER NOT NULL DEFAULT 1,
    created_at           REAL NOT NULL DEFAULT 0.0,
    updated_at           REAL NOT NULL DEFAULT 0.0
)
"""

_CREATE_OUTCOMES = """
CREATE TABLE IF NOT EXISTS profile_outcomes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    profile TEXT NOT NULL,
    r       REAL NOT NULL DEFAULT 0.0,
    ts      REAL NOT NULL DEFAULT 0.0
)
"""

_CREATE_SELECTIONS = """
CREATE TABLE IF NOT EXISTS profile_selections (
    profile TEXT PRIMARY KEY,
    count   INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_IDX_OUT = (
    "CREATE INDEX IF NOT EXISTS idx_prof_out ON profile_outcomes (profile, ts)"
)

# Bound the per-profile outcome history so the table stays small.
_OUTCOME_HISTORY_CAP = 5000


@dataclass
class ExecutionProfile:
    """A complete per-trade execution parameter vector.

    Every field is a knob the rest of the pipeline already understands; a
    profile just bundles a coherent set of them under a name so the system can
    measure, weight, and evolve whole execution styles instead of single knobs.
    """

    name: str
    sl_atr_multiplier: float = 2.0       # SL distance as an ATR multiple
    tp_rr_ratio: float = 2.0             # TP1 reward:risk
    tp2_rr_ratio: float = 4.0            # TP2 reward:risk
    max_hold_bars: int = 0               # max bars before time exit (0 = unlimited)
    trailing_method: str = "structure"   # structure | atr | fixed_pips | breakeven_only | none
    trailing_activation_r: float = 1.0    # R-multiple before trailing/BE activates
    partial_exit_enabled: bool = True     # take a partial at TP1
    partial_exit_r: float = 1.5           # R-multiple for the partial (informational)
    partial_exit_pct: float = 0.33        # fraction closed at the partial
    entry_confirmation: str = "none"      # required M1 confirmation or "none"
    min_score_override: float = 0.0       # entry-score floor (0 = use config default)
    conviction_floor: float = 0.0         # min conviction to prefer this profile
    builtin: bool = False                 # seeded built-in (vs evolution-created)
    active: bool = True                   # disabled profiles are never selected

    def clamp(self) -> "ExecutionProfile":
        """Return a bounds-safe copy (defensive — applied on load / create)."""
        method = str(self.trailing_method or "structure").strip().lower()
        if method not in TRAILING_METHODS:
            method = "structure"
        return ExecutionProfile(
            name=str(self.name),
            sl_atr_multiplier=_clampf(self.sl_atr_multiplier, 0.2, 10.0, 2.0),
            tp_rr_ratio=_clampf(self.tp_rr_ratio, 0.5, 20.0, 2.0),
            tp2_rr_ratio=_clampf(self.tp2_rr_ratio, 0.5, 40.0, 4.0),
            max_hold_bars=max(0, int(_safe_int(self.max_hold_bars, 0))),
            trailing_method=method,
            trailing_activation_r=_clampf(self.trailing_activation_r, 0.0, 20.0, 1.0),
            partial_exit_enabled=bool(self.partial_exit_enabled),
            partial_exit_r=_clampf(self.partial_exit_r, 0.1, 20.0, 1.5),
            partial_exit_pct=_clampf(self.partial_exit_pct, 0.05, 1.0, 0.33),
            entry_confirmation=str(self.entry_confirmation or "none").strip().lower(),
            min_score_override=_clampf(self.min_score_override, 0.0, 100.0, 0.0),
            conviction_floor=_clampf(self.conviction_floor, 0.0, 1.0, 0.0),
            builtin=bool(self.builtin),
            active=bool(self.active),
        )

    # ── Trade-manager override mapping (additive, via existing plan_* hooks) ──

    def plan_trail_strategy(self) -> str:
        """Map ``trailing_method`` to the trade manager's ``plan_trail_strategy``.

        The trade manager only special-cases ``"none"`` to disable trailing
        (BE still snaps independently), so ``breakeven_only`` maps to ``"none"``;
        every other method trails via the structure engine.
        """
        return "none" if self.trailing_method == "breakeven_only" else self.trailing_method

    def plan_partial_ratio(self) -> Optional[float]:
        """Partial-close fraction for the trade manager, or ``None`` to defer.

        ``None`` means "no explicit profile partial" → the trade manager uses
        its global default (it always banks something at TP1). A profile with
        partials disabled returns ``None`` rather than forcing a 100% close.
        """
        if not self.partial_exit_enabled:
            return None
        return _clampf(self.partial_exit_pct, 0.1, 1.0, 0.33)

    def max_hold_exceeded(self, bars_held: float) -> bool:
        """Whether a position older than ``max_hold_bars`` should time-exit.

        Pure helper — ``max_hold_bars`` of 0 means unlimited (never exceeded).
        """
        if int(self.max_hold_bars) <= 0:
            return False
        try:
            return float(bars_held) >= float(self.max_hold_bars)
        except (TypeError, ValueError):
            return False


def _clampf(v, lo: float, hi: float, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(f):
        return float(default)
    return float(max(lo, min(hi, f)))


def _safe_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(default)


# ── Built-in seed profiles ───────────────────────────────────────────────────
# Parameter vectors, NOT strategy identities. They are the starting library the
# evolution layer measures and reshapes. Order is the selection-search order.

def _builtin_profiles() -> List[ExecutionProfile]:
    return [
        ExecutionProfile(
            name="tight_scalp", sl_atr_multiplier=1.0, tp_rr_ratio=1.5,
            tp2_rr_ratio=2.0, max_hold_bars=50, trailing_method="breakeven_only",
            trailing_activation_r=0.5, partial_exit_enabled=True,
            partial_exit_r=1.0, partial_exit_pct=0.5, entry_confirmation="none",
            min_score_override=0.0, conviction_floor=0.0, builtin=True, active=True,
        ),
        ExecutionProfile(
            name="standard_swing", sl_atr_multiplier=2.0, tp_rr_ratio=2.0,
            tp2_rr_ratio=4.0, max_hold_bars=0, trailing_method="structure",
            trailing_activation_r=1.0, partial_exit_enabled=True,
            partial_exit_r=1.5, partial_exit_pct=0.33, entry_confirmation="none",
            min_score_override=0.0, conviction_floor=0.0, builtin=True, active=True,
        ),
        ExecutionProfile(
            name="wide_position", sl_atr_multiplier=3.0, tp_rr_ratio=3.0,
            tp2_rr_ratio=6.0, max_hold_bars=0, trailing_method="structure",
            trailing_activation_r=1.5, partial_exit_enabled=False,
            partial_exit_r=2.0, partial_exit_pct=0.33, entry_confirmation="none",
            min_score_override=0.0, conviction_floor=0.0, builtin=True, active=True,
        ),
        ExecutionProfile(
            name="momentum_chase", sl_atr_multiplier=1.5, tp_rr_ratio=1.5,
            tp2_rr_ratio=3.0, max_hold_bars=100, trailing_method="atr",
            trailing_activation_r=0.5, partial_exit_enabled=True,
            partial_exit_r=1.0, partial_exit_pct=0.5, entry_confirmation="none",
            min_score_override=0.0, conviction_floor=0.0, builtin=True, active=True,
        ),
        ExecutionProfile(
            name="counter_trend_fade", sl_atr_multiplier=1.0, tp_rr_ratio=2.0,
            tp2_rr_ratio=3.0, max_hold_bars=30, trailing_method="breakeven_only",
            trailing_activation_r=0.3, partial_exit_enabled=False,
            partial_exit_r=1.0, partial_exit_pct=0.5, entry_confirmation="none",
            min_score_override=0.0, conviction_floor=0.0, builtin=True, active=True,
        ),
    ]


# Tunable scalar fields per profile (snapshot / rollback / validate cover these).
_TUNABLE_FIELDS = (
    "sl_atr_multiplier",
    "tp_rr_ratio",
    "tp2_rr_ratio",
    "trailing_activation_r",
    "partial_exit_r",
    "partial_exit_pct",
    "min_score_override",
    "conviction_floor",
)

_FIELD_BOUNDS = {
    "sl_atr_multiplier": (0.2, 10.0),
    "tp_rr_ratio": (0.5, 20.0),
    "tp2_rr_ratio": (0.5, 40.0),
    "trailing_activation_r": (0.0, 20.0),
    "partial_exit_r": (0.1, 20.0),
    "partial_exit_pct": (0.05, 1.0),
    "min_score_override": (0.0, 100.0),
    "conviction_floor": (0.0, 1.0),
}


class ExecutionProfileManager(TuningGuardMixin):
    """SQLite-backed library of execution profiles + context-aware selection."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        enabled: bool = True,
        default_profile: str = "standard_swing",
        allow_profile_creation: bool = True,
        max_active_profiles: int = 10,
        min_trades_for_scoring: int = 30,
        strong_consensus_threshold: float = 0.75,
        weak_consensus_threshold: float = 0.45,
    ) -> None:
        self.enabled = bool(enabled)
        self.default_profile = str(default_profile or "standard_swing")
        self.allow_profile_creation = bool(allow_profile_creation)
        self.max_active_profiles = max(1, int(max_active_profiles))
        self.min_trades_for_scoring = max(1, int(min_trades_for_scoring))
        self.strong_consensus_threshold = _clampf(strong_consensus_threshold, 0.0, 1.0, 0.75)
        self.weak_consensus_threshold = _clampf(weak_consensus_threshold, 0.0, 1.0, 0.45)

        self._db_path = Path(db_path) if db_path is not None else _DB_PATH
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._profiles: Dict[str, ExecutionProfile] = {}
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ExecutionProfiles] could not create db dir: {}", exc)
        self._connect()
        self._seed_builtins()
        self._load_profiles()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def _connect(self) -> None:
        try:
            self._conn = sqlite3.connect(
                str(self._db_path), timeout=10, check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(_CREATE_PROFILES)
            self._conn.execute(_CREATE_OUTCOMES)
            self._conn.execute(_CREATE_SELECTIONS)
            self._conn.execute(_CREATE_IDX_OUT)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ExecutionProfiles] DB connect/init failed ({}): {}", self._db_path, exc)
            self._conn = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    logger.debug("[ExecutionProfiles] conn.close() failed during cleanup")
                self._conn = None

    def _seed_builtins(self) -> None:
        """Insert the built-in profiles once (idempotent — never overwrites)."""
        if self._conn is None:
            return
        with self._lock:
            try:
                existing = {
                    str(r[0]) for r in self._conn.execute("SELECT name FROM profiles").fetchall()
                }
                now = time.time()
                for p in _builtin_profiles():
                    if p.name in existing:
                        continue
                    self._insert_profile(p.clamp(), now)
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[ExecutionProfiles] seed failed: {}", exc)

    def _insert_profile(self, p: ExecutionProfile, now: float) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO profiles
               (name, sl_atr_multiplier, tp_rr_ratio, tp2_rr_ratio, max_hold_bars,
                trailing_method, trailing_activation_r, partial_exit_enabled,
                partial_exit_r, partial_exit_pct, entry_confirmation,
                min_score_override, conviction_floor, builtin, active,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                p.name, p.sl_atr_multiplier, p.tp_rr_ratio, p.tp2_rr_ratio,
                int(p.max_hold_bars), p.trailing_method, p.trailing_activation_r,
                1 if p.partial_exit_enabled else 0, p.partial_exit_r,
                p.partial_exit_pct, p.entry_confirmation, p.min_score_override,
                p.conviction_floor, 1 if p.builtin else 0, 1 if p.active else 0,
                now, now,
            ),
        )

    def _load_profiles(self) -> None:
        if self._conn is None:
            # Memory-only fallback so selection still works without persistence.
            self._profiles = {p.name: p.clamp() for p in _builtin_profiles()}
            return
        with self._lock:
            try:
                cur = self._conn.execute(
                    """SELECT name, sl_atr_multiplier, tp_rr_ratio, tp2_rr_ratio,
                              max_hold_bars, trailing_method, trailing_activation_r,
                              partial_exit_enabled, partial_exit_r, partial_exit_pct,
                              entry_confirmation, min_score_override, conviction_floor,
                              builtin, active
                       FROM profiles"""
                )
                profiles: Dict[str, ExecutionProfile] = {}
                for row in cur.fetchall():
                    p = ExecutionProfile(
                        name=str(row[0]), sl_atr_multiplier=row[1], tp_rr_ratio=row[2],
                        tp2_rr_ratio=row[3], max_hold_bars=row[4], trailing_method=row[5],
                        trailing_activation_r=row[6], partial_exit_enabled=bool(row[7]),
                        partial_exit_r=row[8], partial_exit_pct=row[9],
                        entry_confirmation=row[10], min_score_override=row[11],
                        conviction_floor=row[12], builtin=bool(row[13]), active=bool(row[14]),
                    ).clamp()
                    profiles[p.name] = p
                if profiles:
                    self._profiles = profiles
                else:
                    self._profiles = {p.name: p.clamp() for p in _builtin_profiles()}
            except Exception as exc:  # noqa: BLE001
                logger.debug("[ExecutionProfiles] _load_profiles failed: {}", exc)
                if not self._profiles:
                    self._profiles = {p.name: p.clamp() for p in _builtin_profiles()}

    # ── Library access ─────────────────────────────────────────────────────────

    def get_profile(self, name: str) -> Optional[ExecutionProfile]:
        """Lookup by name (returns the in-memory copy). ``None`` if unknown."""
        return self._profiles.get(str(name or ""))

    def active_profiles(self) -> List[ExecutionProfile]:
        return [p for p in self._profiles.values() if p.active]

    def create_profile(self, profile: ExecutionProfile) -> Optional[ExecutionProfile]:
        """Add a new (evolution-created) profile, honouring the active cap.

        Blocked when creation is disabled or the active cap is reached. Returns
        the stored profile on success, else ``None``.
        """
        if not self.allow_profile_creation:
            logger.debug("[ExecutionProfiles] creation disabled — refused {}", profile.name)
            return None
        p = profile.clamp()
        if not p.name or p.name in self._profiles:
            logger.debug("[ExecutionProfiles] create refused — bad/dupe name {!r}", p.name)
            return None
        if p.active and len(self.active_profiles()) >= self.max_active_profiles:
            logger.debug(
                "[ExecutionProfiles] create refused — active cap {} reached",
                self.max_active_profiles,
            )
            return None
        with self._lock:
            self._profiles[p.name] = p
            if self._conn is not None:
                try:
                    self._insert_profile(p, time.time())
                    self._conn.commit()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[ExecutionProfiles] create persist failed: {}", exc)
        logger.info("[ExecutionProfiles] created profile {}", p.name)
        return p

    def retire_profile(self, name: str) -> bool:
        """Mark a profile inactive (kept for history; never selected)."""
        p = self._profiles.get(str(name or ""))
        if p is None:
            return False
        retired = ExecutionProfile(**{**asdict(p), "active": False})
        with self._lock:
            self._profiles[p.name] = retired
            if self._conn is not None:
                try:
                    self._conn.execute(
                        "UPDATE profiles SET active=0, updated_at=? WHERE name=?",
                        (time.time(), p.name),
                    )
                    self._conn.commit()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[ExecutionProfiles] retire persist failed: {}", exc)
        logger.info("[ExecutionProfiles] retired profile {}", p.name)
        return True

    # ── Selection (read path — always allowed) ──────────────────────────────────

    def select_profile(
        self,
        *,
        horizon: str = "",
        regime: str = "",
        consensus_strength: float = 0.5,
    ) -> Optional[ExecutionProfile]:
        """Choose the execution profile for a trade from its market context.

        Deterministic, instrument-agnostic mapping:

        * ranker horizon sets the base style (SCALP → fast, SWING → slow, no
          horizon → the configured default);
        * a strongly-trending regime with strong consensus widens a SWING idea
          and pushes a SCALP idea toward momentum-chase;
        * a ranging regime with weak consensus prefers the counter-trend fade.

        Returns ``None`` when the manager is disabled (the caller then uses its
        config-level defaults — a true no-op). Falls back to the default
        profile, then any active profile, if a chosen name is missing/retired.
        """
        if not self.enabled:
            return None
        h = str(horizon or "").strip().upper()
        regime_u = str(regime or "").strip().upper()
        try:
            cs = float(consensus_strength)
        except (TypeError, ValueError):
            cs = 0.5
        strong = cs >= self.strong_consensus_threshold
        weak = cs <= self.weak_consensus_threshold
        trending = "TREND" in regime_u
        volatile = "VOLATIL" in regime_u
        ranging = "RANG" in regime_u

        if h == "SCALP":
            name = "momentum_chase" if (trending or volatile) else "tight_scalp"
        elif h == "SWING":
            name = "wide_position" if (trending and strong) else "standard_swing"
        else:
            name = self.default_profile

        # A ranging book with weak agreement is a mean-reversion context — fade.
        if ranging and weak:
            name = "counter_trend_fade"

        profile = self._resolve_active(name)
        if profile is not None:
            self._record_selection(profile.name)
        return profile

    def _resolve_active(self, name: str) -> Optional[ExecutionProfile]:
        """Return the named profile if active, else the default, else any active."""
        p = self._profiles.get(str(name or ""))
        if p is not None and p.active:
            return p
        d = self._profiles.get(self.default_profile)
        if d is not None and d.active:
            return d
        actives = self.active_profiles()
        return actives[0] if actives else None

    def _record_selection(self, name: str) -> None:
        if self._conn is None or not name:
            return
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO profile_selections (profile, count) VALUES (?, 1)
                       ON CONFLICT(profile) DO UPDATE SET count = count + 1""",
                    (name,),
                )
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[ExecutionProfiles] selection record failed: {}", exc)

    # ── Outcome ingestion (read/learn path — always allowed) ─────────────────────

    def record_outcome(
        self, profile_name: str, r_multiple: float, *, timestamp: Optional[float] = None,
    ) -> None:
        """Record a closed trade's realised R against the profile it used.

        Pure data ingestion (never a tuning op). Drives the per-profile
        expectancy shown on the dashboard; the Capital Allocator scores the same
        outcomes via the enriched fingerprint. Bounded to the recent history cap.
        """
        if self._conn is None or not profile_name:
            return
        try:
            r = float(r_multiple)
        except (TypeError, ValueError):
            return
        if not math.isfinite(r):
            return
        ts = float(timestamp) if timestamp is not None else time.time()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO profile_outcomes (profile, r, ts) VALUES (?,?,?)",
                    (str(profile_name), r, ts),
                )
                self._conn.execute(
                    """DELETE FROM profile_outcomes
                       WHERE profile=? AND id NOT IN (
                           SELECT id FROM profile_outcomes
                           WHERE profile=? ORDER BY ts DESC, id DESC LIMIT ?
                       )""",
                    (str(profile_name), str(profile_name), _OUTCOME_HISTORY_CAP),
                )
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[ExecutionProfiles] record_outcome failed: {}", exc)

    def _profile_stats(self) -> Dict[str, dict]:
        """Per-profile (trades, expectancy, selection count) for the dashboard."""
        stats: Dict[str, dict] = {}
        if self._conn is None:
            return stats
        try:
            for prof, n, total in self._conn.execute(
                "SELECT profile, COUNT(*), COALESCE(SUM(r), 0.0) FROM profile_outcomes GROUP BY profile"
            ).fetchall():
                n_i = int(n or 0)
                stats[str(prof)] = {
                    "trades": n_i,
                    "expectancy": (float(total) / n_i) if n_i else 0.0,
                }
            for prof, cnt in self._conn.execute(
                "SELECT profile, count FROM profile_selections"
            ).fetchall():
                stats.setdefault(str(prof), {"trades": 0, "expectancy": 0.0})
                stats[str(prof)]["selections"] = int(cnt or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ExecutionProfiles] _profile_stats failed: {}", exc)
        return stats

    def total_trades(self) -> int:
        if self._conn is None:
            return 0
        try:
            row = self._conn.execute("SELECT COUNT(*) FROM profile_outcomes").fetchone()
            return int(row[0]) if row and row[0] else 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ExecutionProfiles] total_trades failed: {}", exc)
            return 0

    # ── Tunable surface (TunerAgent adapter snapshots / restores these) ──────────

    def get_current_params(self) -> dict:
        """Flat {``profile.field``: value} of every tunable scalar, active first."""
        params: dict = {}
        for name, p in self._profiles.items():
            if not p.active:
                continue
            for fld in _TUNABLE_FIELDS:
                params[f"{name}.{fld}"] = round(float(getattr(p, fld)), 6)
        return params

    def apply_params(self, params: dict) -> None:
        """Apply a flat {``profile.field``: value} params dict (tune / rollback)."""
        if not params:
            return
        with self._lock:
            dirty: set[str] = set()
            for key, val in params.items():
                if "." not in str(key):
                    continue
                name, _, fld = str(key).partition(".")
                if fld not in _TUNABLE_FIELDS:
                    continue
                p = self._profiles.get(name)
                if p is None:
                    continue
                lo, hi = _FIELD_BOUNDS.get(fld, (-1e9, 1e9))
                setattr(p, fld, _clampf(val, lo, hi, float(getattr(p, fld))))
                dirty.add(name)
            for name in dirty:
                self._profiles[name] = self._profiles[name].clamp()
                if self._conn is not None:
                    try:
                        self._insert_profile(self._profiles[name], time.time())
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("[ExecutionProfiles] apply persist failed: {}", exc)
            if dirty and self._conn is not None:
                try:
                    self._conn.commit()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[ExecutionProfiles] apply commit failed: {}", exc)

    # ── State (dashboard) ────────────────────────────────────────────────────────

    def get_state(self) -> dict:
        """Read-only snapshot for the dashboard."""
        stats = self._profile_stats()
        total = self.total_trades()
        rows: List[dict] = []
        for name, p in self._profiles.items():
            st = stats.get(name, {})
            n = int(st.get("trades", 0) or 0)
            rows.append({
                "name": name,
                "active": bool(p.active),
                "builtin": bool(p.builtin),
                "sl_atr_multiplier": round(p.sl_atr_multiplier, 3),
                "tp_rr_ratio": round(p.tp_rr_ratio, 3),
                "tp2_rr_ratio": round(p.tp2_rr_ratio, 3),
                "max_hold_bars": int(p.max_hold_bars),
                "trailing_method": p.trailing_method,
                "trailing_activation_r": round(p.trailing_activation_r, 3),
                "partial_exit_enabled": bool(p.partial_exit_enabled),
                "partial_exit_pct": round(p.partial_exit_pct, 3),
                "entry_confirmation": p.entry_confirmation,
                "min_score_override": round(p.min_score_override, 3),
                "conviction_floor": round(p.conviction_floor, 3),
                "trades": n,
                "selections": int(st.get("selections", 0) or 0),
                "expectancy": round(float(st.get("expectancy", 0.0)), 4),
                "scored": n >= self.min_trades_for_scoring,
            })
        rows.sort(key=lambda r: (not r["active"], -r["selections"], r["name"]))
        return {
            "enabled": self.enabled,
            "source": "live",
            "default_profile": self.default_profile,
            "allow_profile_creation": self.allow_profile_creation,
            "max_active_profiles": self.max_active_profiles,
            "min_trades_for_scoring": self.min_trades_for_scoring,
            "active_count": len(self.active_profiles()),
            "profile_count": len(rows),
            "total_trades": total,
            "strong_consensus_threshold": round(self.strong_consensus_threshold, 3),
            "weak_consensus_threshold": round(self.weak_consensus_threshold, 3),
            "profiles": rows,
        }


__all__ = [
    "ExecutionProfile",
    "ExecutionProfileManager",
    "TRAILING_METHODS",
]
