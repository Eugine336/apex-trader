"""
APEX TRADER — Regime Detection Engine (L7)

Every adaptive layer below this one makes decisions as if "the market is the
market." But markets cycle through distinct regimes — trending, mean-reverting,
volatile, quiet — and a behaviour that prints money in one regime bleeds it in
another. L7 names the *current* regime per pair and makes that context available
to every other layer (execution profiles, capital allocator, vote calibrator,
behaviour discovery, governor) so they can adapt instead of averaging across
regimes they can't see.

Classification is rule-based on a handful of pure-numpy-free price signals
(no sklearn / scipy / torch — standard library + math only), each normalised to
``[0, 1]``:

    * directional strength  — smoothed |+DI − −DI| / (+DI + −DI) (ADX-like)
    * volatility ratio      — short-window stdev / long-window stdev
    * mean-reversion score  — −lag-1 autocorrelation of returns (high ⇒ reverting)
    * range compression     — recent ATR-like range / long-window range

A rule cascade maps those to one of ``TRENDING_UP``, ``TRENDING_DOWN``,
``RANGING``, ``VOLATILE``, ``QUIET``, ``UNKNOWN`` with a ``[0, 1]`` confidence.
Per-pair: different instruments live in different regimes simultaneously.

A :class:`RegimeTracker` adds hysteresis — the committed regime only flips after
``hysteresis_bars`` consecutive bars agree on a new label — so a single spike
never re-labels the market. Transitions are logged with confidence and duration.

The engine never blocks, sizes, or directs a trade. It only *describes* the
market and offers suggested per-module-type weight nudges and a soft
``should_trade`` opinion that consumers may use or ignore. With no history every
pair resolves to ``UNKNOWN`` at low confidence, so the system behaves exactly as
it did before L7 until evidence exists.

Leaf module — standard library + loguru only (plus the leaf ``adaptive.tunable``
guard mixin), so anything may depend on it without an import cycle.
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
_DB_PATH = _DB_DIR / "regime_detection.db"

# Canonical regime labels. UNKNOWN is the cold-start / low-confidence sentinel.
REGIME_TRENDING_UP = "TRENDING_UP"
REGIME_TRENDING_DOWN = "TRENDING_DOWN"
REGIME_RANGING = "RANGING"
REGIME_VOLATILE = "VOLATILE"
REGIME_QUIET = "QUIET"
REGIME_UNKNOWN = "UNKNOWN"

ALL_REGIMES = (
    REGIME_TRENDING_UP,
    REGIME_TRENDING_DOWN,
    REGIME_RANGING,
    REGIME_VOLATILE,
    REGIME_QUIET,
    REGIME_UNKNOWN,
)

# Coarse module-type → regime affinity table used to suggest weight nudges.
# Multipliers are deliberately gentle (consumers may ignore them entirely).
_TREND_MODULES = ("structure", "momentum", "currency_strength", "vwap")
_REVERSION_MODULES = ("order_block", "fvg", "liquidity", "wyckoff")

_CREATE_STATES = """
CREATE TABLE IF NOT EXISTS regime_states (
    pair        TEXT PRIMARY KEY,
    regime      TEXT NOT NULL DEFAULT 'UNKNOWN',
    confidence  REAL NOT NULL DEFAULT 0.0,
    since_ts    REAL NOT NULL DEFAULT 0.0,
    updated_at  REAL NOT NULL DEFAULT 0.0
)
"""

_CREATE_TRANSITIONS = """
CREATE TABLE IF NOT EXISTS regime_transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pair        TEXT NOT NULL,
    old_regime  TEXT NOT NULL DEFAULT 'UNKNOWN',
    new_regime  TEXT NOT NULL DEFAULT 'UNKNOWN',
    confidence  REAL NOT NULL DEFAULT 0.0,
    ts          REAL NOT NULL DEFAULT 0.0
)
"""

_CREATE_PERFORMANCE = """
CREATE TABLE IF NOT EXISTS regime_performance (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    pair    TEXT NOT NULL,
    regime  TEXT NOT NULL DEFAULT 'UNKNOWN',
    r       REAL NOT NULL DEFAULT 0.0,
    ts      REAL NOT NULL DEFAULT 0.0
)
"""

_CREATE_IDX_TRANS = (
    "CREATE INDEX IF NOT EXISTS idx_regime_trans_pair_ts "
    "ON regime_transitions (pair, ts)"
)
_CREATE_IDX_PERF = (
    "CREATE INDEX IF NOT EXISTS idx_regime_perf_regime "
    "ON regime_performance (regime, ts)"
)


@dataclass
class RegimeSignals:
    """The normalised [0,1] feature vector behind a classification."""

    directional_strength: float = 0.0
    direction_sign: float = 0.0  # +1 up, -1 down, 0 flat
    volatility_ratio: float = 0.0
    mean_reversion: float = 0.0
    range_compression: float = 0.0


@dataclass
class RegimeState:
    """The committed regime for a pair (what consumers read)."""

    pair: str
    regime: str = REGIME_UNKNOWN
    confidence: float = 0.0
    since_ts: float = 0.0
    duration_sec: float = 0.0
    signals: RegimeSignals = field(default_factory=RegimeSignals)

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "regime": self.regime,
            "confidence": round(float(self.confidence), 4),
            "since_ts": float(self.since_ts) or None,
            "duration_sec": round(float(self.duration_sec), 1),
            "signals": {
                "directional_strength": round(self.signals.directional_strength, 4),
                "direction_sign": round(self.signals.direction_sign, 2),
                "volatility_ratio": round(self.signals.volatility_ratio, 4),
                "mean_reversion": round(self.signals.mean_reversion, 4),
                "range_compression": round(self.signals.range_compression, 4),
            },
        }


# ── Pure helpers (no numpy) ────────────────────────────────────────────────


def _mean(values: Sequence[float]) -> float:
    return (sum(values) / len(values)) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = _mean(values)
    var = sum((v - m) ** 2 for v in values) / (n - 1)
    return math.sqrt(var) if var > 0 else 0.0


def _clip01(x: float) -> float:
    if not math.isfinite(x):
        return 0.0
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _closes(candles: Sequence) -> List[float]:
    """Extract finite close prices from candles.

    Accepts a bare numeric series (list of close floats), dict rows
    (``{"close": ...}`` / ``{"c": ...}``), OHLC tuples/lists, or objects with a
    ``.close`` attribute — so callers can pass whatever they already have.
    """
    out: List[float] = []
    for c in candles or []:
        v = None
        if isinstance(c, bool):
            continue
        if isinstance(c, (int, float)):
            v = c
        elif isinstance(c, dict):
            v = c.get("close", c.get("c"))
        elif isinstance(c, (list, tuple)) and len(c) >= 4:
            v = c[3]  # OHLC → close
        else:
            v = getattr(c, "close", None)
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f)
    return out


class RegimeDetector(TuningGuardMixin):
    """Per-pair market-regime classifier with hysteresis + SQLite persistence."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        enabled: bool = True,
        lookback_bars: int = 50,
        hysteresis_bars: int = 5,
        volatility_short_window: int = 10,
        volatility_long_window: int = 50,
        adx_period: int = 14,
        autocorrelation_lag: int = 1,
        trending_threshold: float = 0.6,
        volatile_threshold: float = 0.7,
        quiet_threshold: float = 0.3,
    ) -> None:
        self.enabled = bool(enabled)
        self._db_path = Path(db_path) if db_path is not None else _DB_PATH
        # Tunable knobs (mutable; the TunerAgent adapter snapshots these).
        self.lookback_bars = max(5, int(lookback_bars))
        self.hysteresis_bars = max(1, int(hysteresis_bars))
        self.volatility_short_window = max(2, int(volatility_short_window))
        self.volatility_long_window = max(self.volatility_short_window + 1, int(volatility_long_window))
        self.adx_period = max(2, int(adx_period))
        self.autocorrelation_lag = max(1, int(autocorrelation_lag))
        self.trending_threshold = _clip01(float(trending_threshold))
        self.volatile_threshold = _clip01(float(volatile_threshold))
        self.quiet_threshold = _clip01(float(quiet_threshold))

        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        # Committed state per pair + the per-pair candidate streak for hysteresis.
        self._states: Dict[str, RegimeState] = {}
        self._pending: Dict[str, Tuple[str, int]] = {}  # pair -> (candidate, streak)
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[RegimeDetector] could not create db dir: {}", exc)
        self._connect()
        self._load_state()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def _connect(self) -> None:
        # Connect with one retry on failure. A persistent connect/init failure
        # raises rather than silently degrading to an in-memory zombie: the
        # caller (SystemContext.create) catches it and leaves the subsystem
        # None, which loses no durable state silently and is visible at startup.
        last_exc: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                self._conn = sqlite3.connect(
                    str(self._db_path), timeout=10, check_same_thread=False,
                )
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute(_CREATE_STATES)
                self._conn.execute(_CREATE_TRANSITIONS)
                self._conn.execute(_CREATE_PERFORMANCE)
                self._conn.execute(_CREATE_IDX_TRANS)
                self._conn.execute(_CREATE_IDX_PERF)
                self._conn.commit()
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self._conn = None
                logger.error(
                    "[RegimeDetector] DB connect/init failed (attempt {}/2) ({}): {}",
                    attempt, self._db_path, exc,
                )
                if attempt == 1:
                    time.sleep(1.0)
        raise RuntimeError(
            f"RegimeDetector DB connect failed after retry ({self._db_path}): {last_exc}"
        )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    logger.debug("[RegimeDetector] conn.close() failed during cleanup")
                self._conn = None

    def _load_state(self) -> None:
        if self._conn is None:
            return
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT pair, regime, confidence, since_ts FROM regime_states"
                )
                for pair, regime, conf, since in cur.fetchall():
                    self._states[str(pair)] = RegimeState(
                        pair=str(pair),
                        regime=str(regime) if regime in ALL_REGIMES else REGIME_UNKNOWN,
                        confidence=float(conf),
                        since_ts=float(since),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[RegimeDetector] _load_state failed: {}", exc)

    # ── Feature extraction ─────────────────────────────────────────────────────

    def _compute_signals(self, closes: Sequence[float]) -> RegimeSignals:
        """Derive the normalised regime feature vector from a close series."""
        sig = RegimeSignals()
        n = len(closes)
        if n < 3:
            return sig
        window = list(closes[-self.lookback_bars:])
        returns = [window[i] - window[i - 1] for i in range(1, len(window))]
        if not returns:
            return sig

        # Directional strength (ADX-like): net displacement vs total path length.
        ups = sum(r for r in returns if r > 0)
        downs = -sum(r for r in returns if r < 0)
        gross = ups + downs
        if gross > 0:
            sig.directional_strength = _clip01(abs(ups - downs) / gross)
            sig.direction_sign = 1.0 if ups >= downs else -1.0
        else:
            sig.directional_strength = 0.0
            sig.direction_sign = 0.0

        # Volatility ratio: recent stdev vs long stdev (1.0 ⇒ same, >1 ⇒ expanding).
        short = window[-self.volatility_short_window:]
        long = window[-self.volatility_long_window:]
        short_v = _stdev(short)
        long_v = _stdev(long)
        if long_v > 1e-12:
            sig.volatility_ratio = _clip01((short_v / long_v) / 2.0)  # /2 → ratio of 2.0 saturates
        else:
            sig.volatility_ratio = 0.0

        # Mean-reversion: negative lag-k autocorrelation of returns ⇒ reverting.
        ac = self._autocorr(returns, self.autocorrelation_lag)
        sig.mean_reversion = _clip01((-ac + 1.0) / 2.0)  # ac=-1 → 1.0, ac=+1 → 0.0

        # Range compression: recent absolute-range vs long absolute-range.
        recent_range = (max(short) - min(short)) if short else 0.0
        long_range = (max(long) - min(long)) if long else 0.0
        if long_range > 1e-12:
            sig.range_compression = _clip01(1.0 - (recent_range / long_range))
        else:
            sig.range_compression = 0.0
        return sig

    @staticmethod
    def _autocorr(returns: Sequence[float], lag: int) -> float:
        n = len(returns)
        if n <= lag + 1:
            return 0.0
        m = _mean(returns)
        denom = sum((r - m) ** 2 for r in returns)
        if denom <= 1e-12:
            return 0.0
        num = sum((returns[i] - m) * (returns[i - lag] - m) for i in range(lag, n))
        ac = num / denom
        if not math.isfinite(ac):
            return 0.0
        return max(-1.0, min(1.0, ac))

    # ── Classification ──────────────────────────────────────────────────────────

    def _classify(self, sig: RegimeSignals) -> Tuple[str, float]:
        """Rule cascade: feature vector → (regime, confidence).

        Trending is checked *first*: a clean directional move has low volatility
        and (because the long window spans a big displacement) high range
        compression, which would otherwise be mistaken for a quiet market. A
        strong, signed directional read settles it before those weaker signals
        are considered.
        """
        # Trending: strong, persistent directional strength dominates everything.
        if sig.directional_strength >= self.trending_threshold and sig.direction_sign != 0.0:
            regime = REGIME_TRENDING_UP if sig.direction_sign > 0 else REGIME_TRENDING_DOWN
            return regime, _clip01(sig.directional_strength)

        # Volatile: expanding short-term vol with weak direction.
        if sig.volatility_ratio >= self.volatile_threshold:
            return REGIME_VOLATILE, _clip01(sig.volatility_ratio)

        # Quiet: very low volatility AND a compressed recent range.
        if sig.volatility_ratio <= self.quiet_threshold and sig.range_compression >= (1.0 - self.quiet_threshold):
            quiet_score = (1.0 - sig.volatility_ratio) * 0.5 + sig.range_compression * 0.5
            return REGIME_QUIET, _clip01(quiet_score)

        # Ranging: mean-reverting and not strongly directional.
        if sig.mean_reversion >= 0.5 and sig.directional_strength < self.trending_threshold:
            return REGIME_RANGING, _clip01(sig.mean_reversion)

        # No clear winner — fall back, scored by the weak directional read.
        return REGIME_RANGING, _clip01(max(0.0, 0.5 - sig.directional_strength))

    # ── Update (data ingestion — always allowed) ─────────────────────────────────

    def update(self, pair: str, candles: Sequence) -> RegimeState:
        """Re-classify ``pair`` from its candle history and apply hysteresis.

        Pure observation — never blocked by the TunerAgent. Returns the
        *committed* RegimeState (which only flips after ``hysteresis_bars``
        agreeing observations). Safe + no-op-ish when disabled or thin: a short
        series yields UNKNOWN at zero confidence.
        """
        pair = str(pair or "")
        if not pair:
            return RegimeState(pair="")
        if not self.enabled:
            return self._states.get(pair, RegimeState(pair=pair))

        closes = _closes(candles)
        sig = self._compute_signals(closes)
        if len(closes) < max(3, self.volatility_short_window):
            candidate, conf = REGIME_UNKNOWN, 0.0
        else:
            candidate, conf = self._classify(sig)

        with self._lock:
            committed = self._states.get(pair)
            now = time.time()
            if committed is None:
                committed = RegimeState(pair=pair, regime=REGIME_UNKNOWN, since_ts=now)
                self._states[pair] = committed

            # Hysteresis: require N consecutive agreeing observations to flip.
            cand_label, streak = self._pending.get(pair, (candidate, 0))
            if cand_label == candidate:
                streak += 1
            else:
                cand_label, streak = candidate, 1
            self._pending[pair] = (cand_label, streak)

            flipped = False
            if candidate != committed.regime and streak >= self.hysteresis_bars:
                old = committed.regime
                committed.regime = candidate
                committed.since_ts = now
                flipped = True
                self._log_transition(pair, old, candidate, conf, now)

            committed.confidence = conf
            committed.signals = sig
            committed.duration_sec = max(0.0, now - committed.since_ts)
            # Persist only when the committed regime actually changes. Confidence
            # and duration are recomputed from candles on restart, so committing
            # them every cycle was pure write amplification on the scan path; the
            # durable fact worth saving is the regime label + its since_ts, which
            # only move on a flip.
            if flipped:
                self._persist_state(committed, now)
                logger.info(
                    "[RegimeDetector] {} regime → {} (conf {:.2f})",
                    pair, committed.regime, conf,
                )
            return committed

    def _log_transition(self, pair: str, old: str, new: str, conf: float, ts: float) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT INTO regime_transitions (pair, old_regime, new_regime, confidence, ts)"
                " VALUES (?,?,?,?,?)",
                (pair, old, new, float(conf), float(ts)),
            )
            # Bound the log.
            self._conn.execute(
                """DELETE FROM regime_transitions WHERE pair=? AND id NOT IN (
                       SELECT id FROM regime_transitions WHERE pair=? ORDER BY ts DESC LIMIT 200
                   )""",
                (pair, pair),
            )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[RegimeDetector] _log_transition failed: {}", exc)

    def _persist_state(self, state: RegimeState, ts: float) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO regime_states (pair, regime, confidence, since_ts, updated_at)"
                " VALUES (?,?,?,?,?)",
                (state.pair, state.regime, float(state.confidence), float(state.since_ts), float(ts)),
            )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[RegimeDetector] _persist_state failed: {}", exc)

    def record_performance(self, pair: str, r_multiple: float) -> None:
        """Credit a closed trade's R to whatever regime the pair is currently in.

        Pure observation — lets the dashboard show which regimes the book makes
        money in. Never blocked by the TunerAgent.
        """
        if self._conn is None or not pair:
            return
        try:
            r = float(r_multiple)
        except (TypeError, ValueError):
            return
        if not math.isfinite(r):
            return
        regime = self._states.get(str(pair), RegimeState(pair=str(pair))).regime
        try:
            self._conn.execute(
                "INSERT INTO regime_performance (pair, regime, r, ts) VALUES (?,?,?,?)",
                (str(pair), regime, r, time.time()),
            )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[RegimeDetector] record_performance failed: {}", exc)

    # ── Consumption (read path — always allowed) ─────────────────────────────────

    def get_regime(self, pair: str) -> RegimeState:
        """Current committed regime for ``pair`` (UNKNOWN if never seen)."""
        st = self._states.get(str(pair))
        if st is None:
            return RegimeState(pair=str(pair), regime=REGIME_UNKNOWN, confidence=0.0)
        st.duration_sec = max(0.0, time.time() - st.since_ts) if st.since_ts else 0.0
        return st

    def get_regime_label(self, pair: str) -> str:
        """Convenience: just the regime string."""
        return self.get_regime(pair).regime

    def should_trade(self, pair: str) -> Tuple[bool, str]:
        """Soft opinion on whether conditions favour trading ``pair``.

        Advisory only — consumers may ignore it. Suggests sitting out a QUIET
        regime identified with reasonable confidence; everything else is a go.
        Disabled / cold → always (True, "regime detection off/cold").
        """
        if not self.enabled:
            return True, "regime detection off"
        st = self.get_regime(pair)
        if st.regime == REGIME_UNKNOWN:
            return True, "regime unknown — no opinion"
        if st.regime == REGIME_QUIET and st.confidence >= self.quiet_threshold:
            return False, f"quiet regime (conf {st.confidence:.2f}) — low expectancy"
        return True, f"{st.regime} (conf {st.confidence:.2f})"

    def get_regime_weights(self, pair: str) -> Dict[str, float]:
        """Suggested per-module weight nudges for the current regime.

        Gentle multipliers around 1.0 keyed by module name. Trending regimes
        nudge trend-following modules up and reversion modules down; ranging
        regimes do the reverse. Empty when disabled / unknown (no nudge).
        """
        out: Dict[str, float] = {}
        if not self.enabled:
            return out
        st = self.get_regime(pair)
        if st.regime in (REGIME_UNKNOWN, REGIME_VOLATILE, REGIME_QUIET):
            return out
        # Scale the nudge by confidence so a weakly-identified regime barely moves.
        amp = 0.3 * _clip01(st.confidence)
        if st.regime in (REGIME_TRENDING_UP, REGIME_TRENDING_DOWN):
            up, down = _TREND_MODULES, _REVERSION_MODULES
        else:  # RANGING
            up, down = _REVERSION_MODULES, _TREND_MODULES
        for m in up:
            out[m] = round(1.0 + amp, 4)
        for m in down:
            out[m] = round(max(0.1, 1.0 - amp), 4)
        return out

    def get_regime_context(self) -> Dict[str, dict]:
        """Full per-pair regime snapshot dict other components can consume."""
        return {p: st.to_dict() for p, st in self._states.items()}

    # ── State (dashboard / tunable) ─────────────────────────────────────────────

    def get_current_params(self) -> dict:
        return {
            "lookback_bars": int(self.lookback_bars),
            "hysteresis_bars": int(self.hysteresis_bars),
            "volatility_short_window": int(self.volatility_short_window),
            "volatility_long_window": int(self.volatility_long_window),
            "adx_period": int(self.adx_period),
            "autocorrelation_lag": int(self.autocorrelation_lag),
            "trending_threshold": round(self.trending_threshold, 6),
            "volatile_threshold": round(self.volatile_threshold, 6),
            "quiet_threshold": round(self.quiet_threshold, 6),
        }

    def apply_params(self, params: dict) -> None:
        if not params:
            return
        if "lookback_bars" in params:
            self.lookback_bars = max(5, int(params["lookback_bars"]))
        if "hysteresis_bars" in params:
            self.hysteresis_bars = max(1, int(params["hysteresis_bars"]))
        if "volatility_short_window" in params:
            self.volatility_short_window = max(2, int(params["volatility_short_window"]))
        if "volatility_long_window" in params:
            self.volatility_long_window = max(
                self.volatility_short_window + 1, int(params["volatility_long_window"])
            )
        if "adx_period" in params:
            self.adx_period = max(2, int(params["adx_period"]))
        if "autocorrelation_lag" in params:
            self.autocorrelation_lag = max(1, int(params["autocorrelation_lag"]))
        if "trending_threshold" in params:
            self.trending_threshold = _clip01(float(params["trending_threshold"]))
        if "volatile_threshold" in params:
            self.volatile_threshold = _clip01(float(params["volatile_threshold"]))
        if "quiet_threshold" in params:
            self.quiet_threshold = _clip01(float(params["quiet_threshold"]))

    def _recent_transitions(self, limit: int = 30) -> List[dict]:
        if self._conn is None:
            return []
        try:
            cur = self._conn.execute(
                "SELECT pair, old_regime, new_regime, confidence, ts FROM regime_transitions"
                " ORDER BY ts DESC LIMIT ?",
                (int(limit),),
            )
            return [
                {
                    "pair": str(p),
                    "old_regime": str(o),
                    "new_regime": str(nw),
                    "confidence": round(float(c), 4),
                    "ts": float(ts),
                }
                for p, o, nw, c, ts in cur.fetchall()
            ]
        except Exception as exc:  # noqa: BLE001
            logger.debug("[RegimeDetector] _recent_transitions failed: {}", exc)
            return []

    def _regime_performance(self) -> List[dict]:
        if self._conn is None:
            return []
        try:
            cur = self._conn.execute(
                "SELECT regime, COUNT(*), AVG(r) FROM regime_performance GROUP BY regime"
            )
            return [
                {
                    "regime": str(reg),
                    "trades": int(n or 0),
                    "avg_r": round(float(avg or 0.0), 4),
                }
                for reg, n, avg in cur.fetchall()
            ]
        except Exception as exc:  # noqa: BLE001
            logger.debug("[RegimeDetector] _regime_performance failed: {}", exc)
            return []

    def get_state(self) -> dict:
        """Read-only snapshot for the dashboard."""
        pairs = []
        for p, st in sorted(self._states.items()):
            pairs.append(self.get_regime(p).to_dict())
        # Distribution of pairs across regimes.
        dist: Dict[str, int] = {}
        for st in self._states.values():
            dist[st.regime] = dist.get(st.regime, 0) + 1
        return {
            "enabled": self.enabled,
            "source": "live",
            "pair_count": len(self._states),
            "regime_distribution": dist,
            "thresholds": {
                "trending": round(self.trending_threshold, 4),
                "volatile": round(self.volatile_threshold, 4),
                "quiet": round(self.quiet_threshold, 4),
            },
            "hysteresis_bars": int(self.hysteresis_bars),
            "lookback_bars": int(self.lookback_bars),
            "pairs": pairs,
            "transitions": self._recent_transitions(),
            "performance": self._regime_performance(),
        }


__all__ = [
    "RegimeDetector",
    "RegimeState",
    "RegimeSignals",
    "REGIME_TRENDING_UP",
    "REGIME_TRENDING_DOWN",
    "REGIME_RANGING",
    "REGIME_VOLATILE",
    "REGIME_QUIET",
    "REGIME_UNKNOWN",
    "ALL_REGIMES",
]
