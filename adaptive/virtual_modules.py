"""
APEX TRADER — Virtual (Synthetic) Voting Modules (L5c lifecycle)

The :class:`~adaptive.signal_discovery.SignalDiscoveryEngine` mines *rules nobody
wrote* — combinations of module conditions (e.g. "structure LONG AND liquidity
LONG AND momentum ABSENT") whose edge persists out-of-sample.  Until now that
was purely advisory: a discovered rule surfaced on a dashboard and a human had
to act on it.

This module turns a discovered rule into a **virtual voting module** that
behaves *exactly* like one of the nine fixed brain modules — it casts a
``(direction, confidence)`` vote computed from the real modules' votes — but
with one critical safety property: it can NEVER influence a live decision until
it has earned its way out of SHADOW.

Each virtual module sits in one of three modes (mirroring the Module Governor):

* ``SHADOW``   — its vote is recorded and graded by the SignalLedger, but its
  weight is forced to 0.0 so it cannot move the consensus.  This is the default
  for every newly-registered synthetic signal.
* ``ACTIVE``   — promoted; its vote carries a real weight and participates in
  the weighted consensus exactly like a fixed module.
* ``DISABLED`` — retired (degraded performance); weight 0.0, vote no longer
  computed, but the record is kept for the audit trail / dashboard.

Safety properties:

* **Inert by default.**  An empty registry (no virtual modules registered)
  produces ZERO change: ``compute_votes`` returns no votes.
* **All-shadow = no influence.**  When every registered module is in SHADOW (or
  the kill switch is off), only zero-weight votes are produced — the live panel
  the consensus sees is byte-for-byte the nine real votes.
* **No cascade.**  A virtual module's vote is computed ONLY from the real
  module votes passed in.  Active virtual votes are never fed back into another
  virtual module's computation.
* **Kill switch.**  When ``enabled`` is False every virtual module is forced to
  weight 0.0 immediately (records stay for history).
* **Restart safety.**  On load, modules persisted as ACTIVE re-enter a
  supervised restart-shadow window so a stale signal cannot trade the instant
  the process comes back up.

Storage mirrors the proven sqlite3-in-WAL pattern used across the adaptive
layer: exception-safe, survives restarts.

Leaf module — standard library + loguru + ``ModuleMode`` (enum) + the pure
``Vote`` dataclass only.  It never imports the live loop.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

from adaptive.module_governor import ModuleMode
from brain.vote_evidence import Vote

# A condition is (module, direction) with direction in LONG / SHORT / ABSENT —
# identical to the SignalDiscoveryEngine's condition vocabulary.
ABSENT = "ABSENT"
Condition = tuple[str, str]

# Every virtual module's emitter name starts with this prefix so it is trivially
# distinguishable from a fixed module everywhere it surfaces (ledger, dashboard).
SYNTH_PREFIX = "synth__"

from runtime_paths import data_dir as _data_dir  # noqa: E402

_DB_DIR = _data_dir()
_DB_PATH = _DB_DIR / "virtual_modules.db"

_CREATE_STATE = """
CREATE TABLE IF NOT EXISTS virtual_modules (
    name                  TEXT PRIMARY KEY,
    definition            TEXT NOT NULL,
    mode                  TEXT NOT NULL DEFAULT 'SHADOW',
    weight                REAL NOT NULL DEFAULT 0.0,
    created_at            REAL NOT NULL DEFAULT 0.0,
    created_trade         INTEGER NOT NULL DEFAULT 0,
    last_transition_time  REAL NOT NULL DEFAULT 0.0,
    reason                TEXT,
    promoted_at           REAL,
    promoted_trade        INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_TRANSITIONS = """
CREATE TABLE IF NOT EXISTS virtual_module_transitions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     REAL NOT NULL,
    name          TEXT NOT NULL,
    old_mode      TEXT NOT NULL,
    new_mode      TEXT NOT NULL,
    weight        REAL NOT NULL DEFAULT 0.0,
    reason        TEXT,
    accuracy      REAL NOT NULL DEFAULT 0.0,
    marginal_r    REAL NOT NULL DEFAULT 0.0,
    sample_size   INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_TRANS_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_vm_trans_ts "
    "ON virtual_module_transitions (timestamp)"
)


# ── Definition ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VirtualModuleDefinition:
    """A discovered rule expressed as a directional voter.

    ``conditions`` is the rule's module-condition set; the module fires (casts a
    vote) only when the live panel satisfies EVERY condition.  ``vote_direction``
    is the directional thesis implied by the rule's LONG/SHORT conditions and
    ``base_confidence`` the confidence it votes with when it fires.
    """

    name: str
    conditions: tuple[Condition, ...]
    vote_direction: str               # "LONG" or "SHORT"
    base_confidence: float            # 0.0 .. 1.0
    source_label: str = ""            # the originating rule label
    win_rate: float = 0.0
    edge: float = 0.0

    def fires(self, panel: dict[str, str]) -> bool:
        """Whether the live vote panel satisfies every condition.

        ``panel`` maps each real module to its directional read (LONG / SHORT /
        ABSENT).  A module that did not cast a confident, weighted vote is
        ABSENT — the SAME definition the rule was mined under.
        """
        for module, direction in self.conditions:
            if panel.get(module, ABSENT) != direction:
                return False
        return True

    def compute_vote(self, panel: dict[str, str]) -> Optional[tuple[str, float]]:
        """Return ``(direction, confidence)`` when the rule fires, else None."""
        if not self.conditions or self.vote_direction not in ("LONG", "SHORT"):
            return None
        if self.fires(panel):
            return self.vote_direction, max(0.0, min(1.0, float(self.base_confidence)))
        return None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "conditions": [list(c) for c in self.conditions],
            "vote_direction": self.vote_direction,
            "base_confidence": round(float(self.base_confidence), 4),
            "source_label": self.source_label,
            "win_rate": round(float(self.win_rate), 4),
            "edge": round(float(self.edge), 4),
        }

    @staticmethod
    def from_dict(d: dict) -> Optional["VirtualModuleDefinition"]:
        try:
            conds = tuple(
                (str(c[0]), str(c[1])) for c in (d.get("conditions") or [])
                if isinstance(c, (list, tuple)) and len(c) == 2
            )
            return VirtualModuleDefinition(
                name=str(d.get("name", "")),
                conditions=conds,
                vote_direction=str(d.get("vote_direction", "")),
                base_confidence=float(d.get("base_confidence", 0.0) or 0.0),
                source_label=str(d.get("source_label", "")),
                win_rate=float(d.get("win_rate", 0.0) or 0.0),
                edge=float(d.get("edge", 0.0) or 0.0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[virtual] definition rebuild failed: {}", exc)
            return None


def make_name(conditions: list[Condition] | tuple[Condition, ...]) -> str:
    """Deterministic, stable name for a rule's condition set (order-free)."""
    body = "__".join(f"{m}:{d}" for m, d in sorted(conditions))
    return f"{SYNTH_PREFIX}{body}"


def definition_from_rule(rule: dict) -> Optional[VirtualModuleDefinition]:
    """Build a directional voter definition from a discovered-rule dict.

    The rule dict is the shape ``DiscoveredRule.to_dict`` produces (``conditions``
    is a list of ``[module, direction]`` with direction in LONG / SHORT /
    ABSENT).  The vote direction is the net of the rule's directional
    conditions; a rule with no directional lean (e.g. all ABSENT, or balanced
    LONG/SHORT) is NOT a usable voter and yields ``None``.
    """
    try:
        raw = rule.get("conditions") or []
        conditions: list[Condition] = [
            (str(c[0]), str(c[1])) for c in raw
            if isinstance(c, (list, tuple)) and len(c) == 2
        ]
    except Exception:  # noqa: BLE001
        return None
    if not conditions:
        return None
    net = 0
    for _m, d in conditions:
        if d == "LONG":
            net += 1
        elif d == "SHORT":
            net -= 1
    if net == 0:
        return None  # no directional thesis — not promotable to a voter
    vote_dir = "LONG" if net > 0 else "SHORT"
    win_rate = float(rule.get("win_rate", 0.0) or 0.0)
    return VirtualModuleDefinition(
        name=make_name(conditions),
        conditions=tuple(conditions),
        vote_direction=vote_dir,
        base_confidence=max(0.0, min(1.0, win_rate)),
        source_label=str(rule.get("label", "")),
        win_rate=win_rate,
        edge=float(rule.get("edge", 0.0) or 0.0),
    )


# ── Record ───────────────────────────────────────────────────────────────


@dataclass
class VirtualModuleRecord:
    """Persistent per-virtual-module state."""

    name: str
    definition: VirtualModuleDefinition
    mode: ModuleMode = ModuleMode.SHADOW
    weight: float = 0.0
    created_at: float = 0.0
    created_trade: int = 0
    last_transition_time: float = 0.0
    reason: str = ""
    promoted_at: Optional[float] = None
    promoted_trade: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "definition": self.definition.to_dict(),
            "mode": self.mode.value if isinstance(self.mode, ModuleMode) else str(self.mode),
            "weight": round(float(self.weight), 4),
            "created_at": self.created_at,
            "created_trade": int(self.created_trade),
            "last_transition_time": self.last_transition_time,
            "reason": self.reason,
            "promoted_at": self.promoted_at,
            "promoted_trade": int(self.promoted_trade),
        }


# ── Registry ─────────────────────────────────────────────────────────────


class VirtualModuleRegistry:
    """Owns the set of virtual voting modules: persistence, mode + weight, and
    the per-cycle vote computation the scanner consults.

    The registry is the single authority for a virtual module's mode/weight; the
    lifecycle policy (shadow → promote → retire) lives in
    :class:`~adaptive.virtual_promotion.VirtualSignalManager`, which is the only
    thing that mutates modes — and that path is TunerAgent-guarded.

    Thread-safe: writes hold ``_lock``; the per-vote read path operates on
    snapshots of the in-memory record map.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        db_path: Optional[Path | str] = None,
        max_active: int = 5,
        restart_shadow_trades: int = 10,
    ) -> None:
        # ``enabled`` is the emergency kill switch: when False every virtual
        # module is forced to weight 0.0 (no live influence) while records are
        # preserved.
        self.enabled = bool(enabled)
        self._max_active = max(0, int(max_active))
        self._restart_shadow_trades = max(0, int(restart_shadow_trades))
        self._records: dict[str, VirtualModuleRecord] = {}
        self._lock = threading.RLock()
        # Modules loaded as ACTIVE start in a supervised restart-shadow window
        # (weight forced 0.0) until enough trades have elapsed this run.
        self._restart_pending: set[str] = set()
        self._restart_baseline_trades: Optional[int] = None
        self._db_path = Path(db_path) if db_path is not None else _DB_PATH
        self._conn: Optional[sqlite3.Connection] = None
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[virtual] could not create db dir: {}", exc)
        self._connect()
        self._load_state()

    # ── Lifecycle / persistence ──────────────────────────────────────────

    def _connect(self) -> None:
        try:
            self._conn = sqlite3.connect(
                str(self._db_path), timeout=10, check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(_CREATE_STATE)
            self._conn.execute(_CREATE_TRANSITIONS)
            self._conn.execute(_CREATE_TRANS_IDX)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[virtual] DB connect/init failed ({}): {}", self._db_path, exc)
            self._conn = None

    def _load_state(self) -> None:
        records: dict[str, VirtualModuleRecord] = {}
        if self._conn is not None:
            try:
                cur = self._conn.execute("SELECT * FROM virtual_modules")
                cols = [d[0] for d in cur.description]
                for row in cur.fetchall():
                    r = dict(zip(cols, row))
                    name = str(r.get("name") or "")
                    if not name:
                        continue
                    definition = VirtualModuleDefinition.from_dict(
                        _loads_dict(r.get("definition"))
                    )
                    if definition is None:
                        continue
                    records[name] = VirtualModuleRecord(
                        name=name,
                        definition=definition,
                        mode=_coerce_mode(r.get("mode")),
                        weight=float(r.get("weight") or 0.0),
                        created_at=float(r.get("created_at") or 0.0),
                        created_trade=int(r.get("created_trade") or 0),
                        last_transition_time=float(r.get("last_transition_time") or 0.0),
                        reason=str(r.get("reason") or ""),
                        promoted_at=_opt_float(r.get("promoted_at")),
                        promoted_trade=int(r.get("promoted_trade") or 0),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[virtual] load_state failed: {}", exc)
        with self._lock:
            self._records = records
            # Restart safety: any ACTIVE module re-enters a supervised shadow
            # window before it is allowed to influence a decision again.
            self._restart_pending = {
                n for n, rec in records.items() if rec.mode == ModuleMode.ACTIVE
            }
            self._restart_baseline_trades = None
        if records:
            logger.info(
                "[virtual] loaded {} virtual module(s) ({} active → restart-shadow)",
                len(records), len(self._restart_pending),
            )

    def _persist(self, rec: VirtualModuleRecord) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                """INSERT OR REPLACE INTO virtual_modules
                   (name, definition, mode, weight, created_at, created_trade,
                    last_transition_time, reason, promoted_at, promoted_trade)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec.name,
                    json.dumps(rec.definition.to_dict(), default=str),
                    rec.mode.value if isinstance(rec.mode, ModuleMode) else str(rec.mode),
                    float(rec.weight or 0.0),
                    float(rec.created_at or 0.0),
                    int(rec.created_trade or 0),
                    float(rec.last_transition_time or 0.0),
                    rec.reason or "",
                    rec.promoted_at,
                    int(rec.promoted_trade or 0),
                ),
            )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[virtual] persist failed: {}", exc)

    def _record_transition(
        self, name: str, old_mode: ModuleMode, new_mode: ModuleMode, weight: float,
        reason: str, accuracy: float, marginal_r: float, sample_size: int,
    ) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                """INSERT INTO virtual_module_transitions
                   (timestamp, name, old_mode, new_mode, weight, reason,
                    accuracy, marginal_r, sample_size)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    time.time(), name,
                    old_mode.value if isinstance(old_mode, ModuleMode) else str(old_mode),
                    new_mode.value if isinstance(new_mode, ModuleMode) else str(new_mode),
                    float(weight or 0.0), reason or "",
                    float(accuracy or 0.0), float(marginal_r or 0.0), int(sample_size or 0),
                ),
            )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[virtual] transition record failed: {}", exc)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    logger.debug("[virtual] conn close failed")
                self._conn = None

    # ── Config ────────────────────────────────────────────────────────────

    @property
    def max_active(self) -> int:
        return self._max_active

    def set_enabled(self, enabled: bool) -> None:
        """Flip the kill switch. When False, ``compute_votes`` yields only
        zero-weight (shadow) votes regardless of stored mode."""
        self.enabled = bool(enabled)

    def set_params(
        self, *, max_active: Optional[int] = None,
        restart_shadow_trades: Optional[int] = None,
    ) -> None:
        with self._lock:
            if max_active is not None:
                self._max_active = max(0, int(max_active))
            if restart_shadow_trades is not None:
                self._restart_shadow_trades = max(0, int(restart_shadow_trades))

    # ── Mutation (lifecycle authority) ────────────────────────────────────

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._records

    def register(
        self, definition: VirtualModuleDefinition, *,
        mode: ModuleMode = ModuleMode.SHADOW, total_trades: int = 0,
        reason: str = "discovered",
    ) -> bool:
        """Register a new virtual module (idempotent on name).

        New modules ALWAYS enter in SHADOW unless explicitly overridden — they
        must earn promotion. Returns False when the name already exists.
        """
        if definition is None or not definition.name:
            return False
        if definition.vote_direction not in ("LONG", "SHORT") or not definition.conditions:
            return False
        with self._lock:
            if definition.name in self._records:
                return False
            now = time.time()
            rec = VirtualModuleRecord(
                name=definition.name,
                definition=definition,
                mode=mode if isinstance(mode, ModuleMode) else _coerce_mode(mode),
                weight=0.0,
                created_at=now,
                created_trade=int(total_trades),
                last_transition_time=now,
                reason=reason,
            )
            self._records[definition.name] = rec
            self._persist(rec)
            self._record_transition(
                rec.name, ModuleMode.DISABLED, rec.mode, 0.0, reason, 0.0, 0.0, 0,
            )
        logger.info("[virtual] registered {} (mode={})", definition.name, rec.mode.value)
        return True

    def promote(
        self, name: str, *, weight: float, total_trades: int = 0, reason: str = "",
        accuracy: float = 0.0, marginal_r: float = 0.0, sample_size: int = 0,
    ) -> bool:
        """Move a SHADOW module to ACTIVE with the given (positive) weight."""
        w = max(0.0, float(weight))
        if w <= 0.0:
            return False
        with self._lock:
            rec = self._records.get(name)
            if rec is None or rec.mode == ModuleMode.ACTIVE:
                return False
            old = rec.mode
            now = time.time()
            rec.mode = ModuleMode.ACTIVE
            rec.weight = w
            rec.last_transition_time = now
            rec.promoted_at = now
            rec.promoted_trade = int(total_trades)
            rec.reason = reason or "promoted"
            self._restart_pending.discard(name)
            self._persist(rec)
            self._record_transition(
                name, old, ModuleMode.ACTIVE, w, rec.reason, accuracy, marginal_r, sample_size,
            )
        logger.info(
            "[virtual] PROMOTED {} → ACTIVE weight={:.3f} ({})", name, w, reason,
        )
        return True

    def disable(
        self, name: str, *, reason: str = "", accuracy: float = 0.0,
        marginal_r: float = 0.0, sample_size: int = 0,
    ) -> bool:
        """Retire a module (DISABLED, weight 0.0). Record kept for history."""
        with self._lock:
            rec = self._records.get(name)
            if rec is None or rec.mode == ModuleMode.DISABLED:
                return False
            old = rec.mode
            rec.mode = ModuleMode.DISABLED
            rec.weight = 0.0
            rec.last_transition_time = time.time()
            rec.reason = reason or "retired"
            self._restart_pending.discard(name)
            self._persist(rec)
            self._record_transition(
                name, old, ModuleMode.DISABLED, 0.0, rec.reason, accuracy, marginal_r, sample_size,
            )
        logger.info("[virtual] RETIRED {} → DISABLED ({})", name, reason)
        return True

    def set_weight(self, name: str, weight: float) -> bool:
        """Adjust an ACTIVE module's weight (e.g. calibrated growth)."""
        w = max(0.0, float(weight))
        with self._lock:
            rec = self._records.get(name)
            if rec is None or rec.mode != ModuleMode.ACTIVE:
                return False
            rec.weight = w
            self._persist(rec)
        return True

    # ── Restart-shadow supervision ────────────────────────────────────────

    def tick_restart_shadow(self, total_trades: int) -> None:
        """Clear the restart-shadow window for ACTIVE modules once enough trades
        have elapsed this run. Driven by the lifecycle manager."""
        with self._lock:
            if not self._restart_pending:
                return
            if self._restart_baseline_trades is None:
                self._restart_baseline_trades = int(total_trades)
                return
            if self._restart_shadow_trades <= 0:
                cleared = set(self._restart_pending)
                self._restart_pending.clear()
            else:
                elapsed = int(total_trades) - int(self._restart_baseline_trades)
                if elapsed < self._restart_shadow_trades:
                    return
                cleared = set(self._restart_pending)
                self._restart_pending.clear()
        if cleared:
            logger.info(
                "[virtual] restart-shadow cleared for {} module(s) — resuming ACTIVE weight",
                len(cleared),
            )

    def _effective_weight(self, rec: VirtualModuleRecord) -> float:
        """Live weight after kill switch + restart-shadow are applied."""
        if not self.enabled:
            return 0.0
        if rec.mode != ModuleMode.ACTIVE:
            return 0.0
        if rec.name in self._restart_pending:
            return 0.0
        return max(0.0, float(rec.weight))

    # ── Hot path: vote computation ────────────────────────────────────────

    @staticmethod
    def _panel_from_votes(real_votes: list[Vote]) -> dict[str, str]:
        """Map each real module to its directional read (LONG/SHORT/ABSENT).

        Mirrors the SignalDiscoveryEngine's condition definition exactly: a
        module contributes a direction only when it cast a confident, weighted
        vote; otherwise it is ABSENT.
        """
        panel: dict[str, str] = {}
        for v in real_votes:
            module = getattr(v, "module", "")
            direction = getattr(v, "direction", "NEUTRAL")
            if not module:
                continue
            try:
                conf = float(getattr(v, "confidence", 0.0) or 0.0)
                weight = float(getattr(v, "weight", 0.0) or 0.0)
            except (TypeError, ValueError):
                conf = weight = 0.0
            if direction in ("LONG", "SHORT") and conf > 0.0 and weight > 0.0:
                panel[module] = direction
            else:
                panel[module] = ABSENT
        return panel

    def compute_votes(
        self, real_votes: list[Vote],
    ) -> tuple[list[Vote], list[Vote]]:
        """Compute virtual module votes from the REAL module votes only.

        Returns ``(active_votes, shadow_votes)``:

        * ``active_votes`` — ACTIVE virtual modules whose rule fired, carrying a
          real (>0) weight; these JOIN the live consensus panel.
        * ``shadow_votes`` — SHADOW (and kill-switched / restart-shadowed)
          virtual modules whose rule fired, carrying weight 0.0; these are
          recorded + graded but NEVER influence the decision.

        No cascade: only ``real_votes`` form the firing panel — virtual votes are
        never inputs to another virtual module. Never raises.
        """
        active: list[Vote] = []
        shadow: list[Vote] = []
        try:
            panel = self._panel_from_votes(real_votes or [])
            with self._lock:
                records = list(self._records.values())
                restart = set(self._restart_pending)
                enabled = self.enabled
            for rec in records:
                if rec.mode == ModuleMode.DISABLED:
                    continue  # retired — no longer computed
                fired = rec.definition.compute_vote(panel)
                if fired is None:
                    continue  # rule did not fire — abstain
                direction, confidence = fired
                live = (
                    enabled
                    and rec.mode == ModuleMode.ACTIVE
                    and rec.name not in restart
                )
                weight = max(0.0, float(rec.weight)) if live else 0.0
                vote = Vote(
                    rec.name, direction, confidence, weight,
                    evidence={
                        "virtual": True,
                        "mode": rec.mode.value,
                        "source": rec.definition.source_label,
                        "shadow": not live,
                    },
                )
                if live and weight > 0.0:
                    active.append(vote)
                else:
                    shadow.append(vote)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[virtual] compute_votes failed: {}", exc)
            return [], []
        return active, shadow

    # ── Reading / dashboard ────────────────────────────────────────────────

    def get(self, name: str) -> Optional[VirtualModuleRecord]:
        with self._lock:
            return self._records.get(name)

    def get_all(self) -> list[VirtualModuleRecord]:
        with self._lock:
            return list(self._records.values())

    def get_by_mode(self, mode: ModuleMode) -> list[VirtualModuleRecord]:
        with self._lock:
            return [r for r in self._records.values() if r.mode == mode]

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._records.values() if r.mode == ModuleMode.ACTIVE)

    def get_status(self) -> dict:
        """Per-module status + counts for the dashboard / ops."""
        with self._lock:
            records = [rec.to_dict() for rec in self._records.values()]
            restart = set(self._restart_pending)
        now = time.time()
        for r in records:
            ts = float(r.get("last_transition_time") or 0.0)
            r["seconds_in_mode"] = round(max(0.0, now - ts), 1) if ts else 0.0
            r["restart_shadow"] = r.get("name") in restart
            # Effective (live) weight after kill switch + restart-shadow.
            if not self.enabled or r.get("mode") != ModuleMode.ACTIVE.value or r.get("name") in restart:
                r["effective_weight"] = 0.0
            else:
                r["effective_weight"] = r.get("weight", 0.0)
        records.sort(key=lambda r: (r["mode"] != ModuleMode.ACTIVE.value, r["name"]))
        counts = {m.value: 0 for m in ModuleMode}
        for r in records:
            counts[r["mode"]] = counts.get(r["mode"], 0) + 1
        return {
            "enabled": self.enabled,
            "max_active": self._max_active,
            "restart_shadow_trades": self._restart_shadow_trades,
            "restart_pending": len(restart),
            "modules": records,
            "counts": counts,
            "module_count": len(records),
        }

    def get_transitions(self, limit: int = 100) -> list[dict]:
        if self._conn is None:
            return []
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT timestamp, name, old_mode, new_mode, weight, reason, "
                    "accuracy, marginal_r, sample_size FROM virtual_module_transitions "
                    "ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            except Exception as exc:  # noqa: BLE001
                logger.debug("[virtual] get_transitions failed: {}", exc)
                return []


# ── Helpers ──────────────────────────────────────────────────────────────


def _coerce_mode(value) -> ModuleMode:
    if isinstance(value, ModuleMode):
        return value
    try:
        return ModuleMode(str(value).upper())
    except (ValueError, AttributeError):
        return ModuleMode.SHADOW


def _opt_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _loads_dict(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except (TypeError, ValueError):
        return {}


__all__ = [
    "ABSENT",
    "SYNTH_PREFIX",
    "Condition",
    "VirtualModuleDefinition",
    "VirtualModuleRecord",
    "VirtualModuleRegistry",
    "make_name",
    "definition_from_rule",
]
