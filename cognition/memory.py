"""APEX TRADER — Persistent institutional memory (Phase H: Constitution Part VII).

Part VII mandates that the system *remembers*: every campaign — the market state
that gave rise to it, the Brain's specification, and the realised outcome plus
autonomous post-mortem — is persisted so future reasoning can consult analogous
history ("have I seen a state like this before, and how did it turn out?").

This module is the institutional memory:

* :class:`CampaignMemoryStore` — a self-contained SQLite store (WAL, thread-safe,
  synchronous under a lock). Campaign closes/opens are infrequent and off the
  hot path, so a background writer thread is unnecessary; a plain locked write
  keeps the store deterministic and trivially offline-testable.
* :func:`fingerprint_from_market_state` / :func:`fingerprint_similarity` — a
  pure, deterministic representation of a :class:`~cognition.contracts.MarketState`
  as a per-domain evidential-polarity vector, and a bounded [0, 1] similarity
  over two such fingerprints. These power similarity retrieval.
* :meth:`CampaignMemoryStore.find_analogues` — given the *current* market state,
  return the most similar *completed* past campaigns with their realised
  outcomes, so the Phase E consolidator can hand the Brain a
  ``historical_analogue`` Evidence.

Two write paths, matched heuristically by ``(symbol, direction)`` (the Brain's
``CampaignSpecification`` ids and the legacy :class:`~brain.campaign.Campaign`
ids live in different spaces, so the state-fingerprint recorded at OPEN and the
outcome recorded at CLOSE are stitched together by symbol+direction, newest open
first):

* :meth:`record_open` — the loop records the state fingerprint + spec when the
  Brain opens a campaign.
* :meth:`record_close` — the campaign registry's finalize hook records the
  terminal outcome + post-mortem.

Pure standard library (``sqlite3`` + ``logging``); fail-safe throughout — a
memory fault must never disturb reasoning or execution.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("apex.cognition.memory")

LONG = "LONG"
SHORT = "SHORT"
FLAT = "FLAT"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clampf(v: Any, lo: float, hi: float, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return min(hi, max(lo, f))


# ── Market-state fingerprint (pure, deterministic) ─────────────────────────────


def fingerprint_from_market_state(market_state: Any, *, now: Optional[float] = None) -> dict:
    """Represent a MarketState as a per-domain evidential *confidence* vector.

    Part XXV — the fingerprint carries no directional reading. It captures which
    evidence domains were active and how strongly (mean confidence per domain),
    so similar market *situations* still match without any LONG/SHORT lean.
    Returns ``{"domains": {domain: mean_confidence}, "confidence": mean,
    "polarity": 0.0}``. Only *fresh* evidence contributes. Pure and fail-safe —
    an empty / unreadable state yields a neutral print.
    """
    dom_conf: dict[str, list] = {}
    confs: list = []
    try:
        fresh = market_state.fresh_evidence(now) if market_state is not None else []
    except Exception:  # noqa: BLE001
        fresh = []
    for e in fresh or []:
        try:
            dom = getattr(getattr(e, "domain", None), "value", None) or str(getattr(e, "domain", "other"))
            c = _clampf(getattr(e, "confidence", 0.0), 0.0, 1.0)
            dom_conf.setdefault(str(dom), []).append(c)
            confs.append(c)
        except Exception:  # noqa: BLE001
            continue
    domains = {d: round(sum(v) / len(v), 4) for d, v in dom_conf.items() if v}
    mean_conf = round(sum(confs) / len(confs), 4) if confs else 0.0
    # ``polarity`` retained at 0.0 for schema stability; it is never directional.
    return {"domains": domains, "confidence": mean_conf, "polarity": 0.0}


def fingerprint_similarity(a: dict, b: dict) -> float:
    """Cosine similarity over two fingerprints' domain vectors, mapped to [0, 1].

    Missing domains count as 0 in the union space. Opposing states (negative
    cosine) map to 0 — they are not analogues. Pure and fail-safe.
    """
    try:
        da = (a or {}).get("domains") or {}
        db = (b or {}).get("domains") or {}
        keys = set(da) | set(db)
        if not keys:
            return 0.0
        dot = sum(float(da.get(k, 0.0)) * float(db.get(k, 0.0)) for k in keys)
        na = sum(float(da.get(k, 0.0)) ** 2 for k in keys) ** 0.5
        nb = sum(float(db.get(k, 0.0)) ** 2 for k in keys) ** 0.5
        if na <= 0.0 or nb <= 0.0:
            return 0.0
        cos = dot / (na * nb)
        return round(max(0.0, min(1.0, cos)), 4)
    except Exception:  # noqa: BLE001
        return 0.0


_CREATE = """
CREATE TABLE IF NOT EXISTS campaign_memory (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id      TEXT,
    symbol           TEXT,
    direction        TEXT,
    opened_ms        INTEGER,
    closed_ms        INTEGER,
    closed           INTEGER DEFAULT 0,
    fingerprint_json TEXT,
    spec_json        TEXT,
    outcome_won      INTEGER,
    realized_pnl     REAL,
    verdict          TEXT,
    reasoning_quality REAL,
    state            TEXT,
    ended_reason     TEXT,
    postmortem_json  TEXT
)
"""
_IDX_SYMDIR = ("CREATE INDEX IF NOT EXISTS idx_cm_symdir "
               "ON campaign_memory (symbol, direction, closed)")
_IDX_CLOSED = "CREATE INDEX IF NOT EXISTS idx_cm_closed ON campaign_memory (closed)"


class CampaignMemoryStore:
    """SQLite-backed institutional memory of campaigns. Fail-safe, thread-safe.

    Synchronous writes under a lock (campaign lifecycle events are infrequent and
    off the hot path). Pass ``db_path=":memory:"`` for tests.
    """

    def __init__(self, db_path: Optional[str] = None, *, max_rows: int = 50_000) -> None:
        self._lock = threading.Lock()
        self._max_rows = max(100, int(max_rows))
        self._conn: Optional[sqlite3.Connection] = None
        self._degraded = False
        self._opens = 0
        self._closes = 0
        if db_path is None:
            try:
                from runtime_paths import data_dir as _data_dir
                base = _data_dir()
                base.mkdir(parents=True, exist_ok=True)
                path = str(base / "apex_campaign_memory.db")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[memory] data dir unavailable, using in-memory store: %s", exc)
                path = ":memory:"
        else:
            path = str(db_path)
        try:
            self._conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(_CREATE)
            self._conn.execute(_IDX_SYMDIR)
            self._conn.execute(_IDX_CLOSED)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            self._degraded = True
            logger.error("[memory] init failed — store degraded: %s", exc)

    # ── Writes ─────────────────────────────────────────────────────────────

    def record_open(
        self,
        *,
        symbol: str,
        direction: str,
        fingerprint: dict,
        campaign_id: str = "",
        spec: Optional[dict] = None,
        now_ms: Optional[int] = None,
    ) -> None:
        """Persist the OPEN-time state fingerprint + spec for a campaign. Fail-safe."""
        if self._conn is None or self._degraded:
            return
        sym = str(symbol or "")
        d = str(direction or "").upper()
        if not sym or d not in (LONG, SHORT):
            return
        try:
            fp_json = json.dumps(fingerprint or {})
            spec_json = json.dumps(spec or {})
        except (TypeError, ValueError):
            fp_json, spec_json = "{}", "{}"
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO campaign_memory "
                    "(campaign_id, symbol, direction, opened_ms, closed, "
                    " fingerprint_json, spec_json) VALUES (?, ?, ?, ?, 0, ?, ?)",
                    (str(campaign_id or ""), sym, d,
                     int(now_ms if now_ms is not None else _now_ms()), fp_json, spec_json),
                )
                self._conn.commit()
                self._opens += 1
                self._prune_locked()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[memory] record_open(%s %s) fault: %s", sym, d, exc)

    def record_close(self, campaign: Any, *, now_ms: Optional[int] = None) -> Optional[dict]:
        """Persist a terminal campaign outcome + post-mortem, stitched to its OPEN.

        Accepts a :class:`~brain.campaign.Campaign` (or any object exposing the
        same ``to_dict()`` shape). Updates the most recent still-open row for the
        campaign's ``(symbol, direction)``; if none exists, inserts an
        outcome-only row. Fail-safe.

        Returns a small dict stitching the OPEN snapshot to this outcome —
        ``{"won", "verdict", "supporting_sources", "entry_confidence"}`` — so the
        caller can feed the Part VIII influence ledger / calibration tracker.
        ``supporting_sources`` / ``entry_confidence`` come from the matched open
        row's spec (empty / None when there was no open snapshot). Returns
        ``None`` only when nothing could be recorded.
        """
        if self._conn is None or self._degraded:
            return None
        try:
            data = campaign.to_dict() if hasattr(campaign, "to_dict") else dict(campaign)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[memory] record_close serialise fault: %s", exc)
            return None
        sym = str(data.get("symbol", "") or "")
        d = str(data.get("direction", "") or "").upper()
        if not sym or d not in (LONG, SHORT):
            return None
        pm = data.get("postmortem") or {}
        won = 1 if bool(pm.get("outcome_won", data.get("realized_pnl", 0.0) > 0)) else 0
        pnl = _clampf(data.get("realized_pnl", 0.0), -1e12, 1e12)
        verdict = str(pm.get("verdict", "") or "")
        rq = _clampf(pm.get("reasoning_quality", 0.0), 0.0, 1.0)
        state = str(data.get("state", "") or "")
        reason = str(data.get("ended_reason", "") or "")
        try:
            pm_json = json.dumps(pm)
        except (TypeError, ValueError):
            pm_json = "{}"
        t = int(now_ms if now_ms is not None else _now_ms())
        stitched = {"won": bool(won), "verdict": verdict,
                    "supporting_sources": [], "entry_confidence": None}
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT id, spec_json FROM campaign_memory WHERE symbol=? AND direction=? "
                    "AND closed=0 ORDER BY opened_ms DESC, id DESC LIMIT 1",
                    (sym, d),
                ).fetchone()
                if row is not None:
                    try:
                        spec = json.loads(row[1]) if row[1] else {}
                        if isinstance(spec, dict):
                            srcs = spec.get("supporting_sources")
                            if isinstance(srcs, list):
                                stitched["supporting_sources"] = [str(s) for s in srcs]
                            ec = spec.get("confidence")
                            if ec is not None:
                                stitched["entry_confidence"] = _clampf(ec, 0.0, 1.0, 0.0)
                    except (TypeError, ValueError):
                        pass
                    self._conn.execute(
                        "UPDATE campaign_memory SET closed=1, closed_ms=?, outcome_won=?, "
                        "realized_pnl=?, verdict=?, reasoning_quality=?, state=?, "
                        "ended_reason=?, postmortem_json=? WHERE id=?",
                        (t, won, pnl, verdict, rq, state, reason, pm_json, int(row[0])),
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO campaign_memory "
                        "(campaign_id, symbol, direction, closed, closed_ms, outcome_won, "
                        " realized_pnl, verdict, reasoning_quality, state, ended_reason, "
                        " postmortem_json, fingerprint_json) "
                        "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, '{}')",
                        (str(data.get("campaign_id", "") or ""), sym, d, t, won, pnl,
                         verdict, rq, state, reason, pm_json),
                    )
                self._conn.commit()
                self._closes += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("[memory] record_close(%s %s) fault: %s", sym, d, exc)
        return stitched

    def _prune_locked(self) -> None:
        """Bound the table to ``max_rows`` (oldest first). Caller holds the lock."""
        try:
            total = self._conn.execute("SELECT COUNT(*) FROM campaign_memory").fetchone()[0]
            if total > self._max_rows:
                excess = total - self._max_rows
                self._conn.execute(
                    "DELETE FROM campaign_memory WHERE id IN "
                    "(SELECT id FROM campaign_memory ORDER BY id ASC LIMIT ?)",
                    (excess,),
                )
                self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[memory] prune fault: %s", exc)

    # ── Retrieval ────────────────────────────────────────────────────────────

    def find_analogues(
        self,
        market_state: Any,
        *,
        direction: Optional[str] = None,
        limit: int = 5,
        min_similarity: float = 0.4,
        same_symbol_only: bool = False,
        now: Optional[float] = None,
    ) -> list:
        """Return the most similar *completed* past campaigns to the current state.

        Compares the current state's fingerprint to every stored completed
        campaign fingerprint; returns up to ``limit`` matches at or above
        ``min_similarity``, each with its realised outcome. Deterministic and
        fail-safe (empty list on any fault or empty store).
        """
        if self._conn is None or self._degraded:
            return []
        try:
            fp = fingerprint_from_market_state(market_state, now=now)
            if not fp.get("domains"):
                return []
            sym = str(getattr(market_state, "symbol", "") or "")
            clauses = ["closed=1", "fingerprint_json IS NOT NULL", "fingerprint_json != '{}'"]
            params: list = []
            if same_symbol_only and sym:
                clauses.append("symbol=?")
                params.append(sym)
            if direction:
                clauses.append("direction=?")
                params.append(str(direction).upper())
            where = " AND ".join(clauses)
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT campaign_id, symbol, direction, fingerprint_json, outcome_won, "
                    f"realized_pnl, verdict, reasoning_quality, state FROM campaign_memory "
                    f"WHERE {where} ORDER BY id DESC LIMIT 2000",
                    params,
                ).fetchall()
            scored = []
            for (cid, rsym, rdir, fpj, won, pnl, verdict, rq, state) in rows:
                try:
                    other = json.loads(fpj) if fpj else {}
                except (TypeError, ValueError):
                    continue
                sim = fingerprint_similarity(fp, other)
                if sim < min_similarity:
                    continue
                scored.append({
                    "campaign_id": cid or "",
                    "symbol": rsym or "",
                    "direction": rdir or "",
                    "similarity": sim,
                    "outcome_won": bool(won),
                    "realized_pnl": round(float(pnl or 0.0), 4),
                    "verdict": verdict or "",
                    "reasoning_quality": round(float(rq or 0.0), 4),
                    "state": state or "",
                })
            scored.sort(key=lambda r: r["similarity"], reverse=True)
            return scored[: max(1, int(limit))]
        except Exception as exc:  # noqa: BLE001
            logger.debug("[memory] find_analogues fault: %s", exc)
            return []

    def count(self, *, closed_only: bool = False) -> int:
        if self._conn is None:
            return 0
        try:
            sql = "SELECT COUNT(*) FROM campaign_memory"
            if closed_only:
                sql += " WHERE closed=1"
            with self._lock:
                return int(self._conn.execute(sql).fetchone()[0])
        except Exception:  # noqa: BLE001
            return 0

    def get_status(self) -> dict:
        return {
            "degraded": self._degraded,
            "opens_recorded": self._opens,
            "closes_recorded": self._closes,
            "rows": self.count(),
            "completed_rows": self.count(closed_only=True),
        }

    def close(self) -> None:
        if self._conn is not None:
            try:
                with self._lock:
                    self._conn.commit()
                    self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None


# ── Module-level singleton ─────────────────────────────────────────────────────

_global: Optional[CampaignMemoryStore] = None
_global_lock = threading.Lock()


def get_campaign_memory(db_path: Optional[str] = None) -> CampaignMemoryStore:
    """Return (or lazily create) the process-wide campaign memory store."""
    global _global
    with _global_lock:
        if _global is None:
            _global = CampaignMemoryStore(db_path=db_path)
        return _global


__all__ = [
    "CampaignMemoryStore",
    "fingerprint_from_market_state",
    "fingerprint_similarity",
    "get_campaign_memory",
]
