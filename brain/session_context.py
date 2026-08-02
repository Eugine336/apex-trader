"""
APEX TRADER — Global Session Context

Classifies the current UTC time into one of four ``TradingSession`` values and
exposes per-session behaviour multipliers, working uniformly across forex,
commodities, indices, crypto and synthetics — never gold-only.

The system already had a rich :class:`~brain.session_engine.SessionEngine`
(session names, liquidity grades, news guard, gold kill zones), but the
opportunistic-trader rewire needs a *lightweight, stateless* classifier whose
only job is to answer "which of the four opportunistic sessions are we in?" and
"how should this instrument behave in that session?". This module fills that
gap and is the foundation for later features (session-aware sizing, session
zone-priority weighting).

Core idea
---------
The trading day is split into four opportunistic sessions (default UTC
windows, all configurable via :class:`SessionWindows` — no magic numbers)::

    ASIAN             21:00 → 07:00   (wraps midnight, low-liquidity chop)
    LONDON            07:00 → 12:00
    NY                12:00 → 21:00
    LONDON_NY_OVERLAP 12:00 → 16:00   (sits INSIDE NY — the best window)

Because the overlap window sits inside the NY window, ``get_session`` checks it
first so the highest-priority (most liquid) session wins.

Per-instrument tuning comes from ``InstrumentProfile``
(``session_size_multipliers`` / ``session_zone_weights``); the global fallbacks
live on ``EntryConfig``; ultimate defaults live here. This mirrors the tuning
resolution used by :class:`~brain.compression_detector.CompressionDetector`.

Thread-safety: the classifier is stateless — every read is a pure function of
the supplied UTC time plus the immutable config — so it needs no locking and is
safe to call concurrently from tick / candle-close worker threads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

from loguru import logger

from brain.instrument_profile import get_profile
from entry.models import EntryConfig


class TradingSession(str, Enum):
    """The four opportunistic trading sessions."""

    ASIAN = "ASIAN"
    LONDON = "LONDON"
    NY = "NY"
    LONDON_NY_OVERLAP = "LONDON_NY_OVERLAP"


# Ultimate fallbacks used only when neither the InstrumentProfile nor the
# EntryConfig supplies a table. Kept here (rather than imported) so this module
# never depends on the profile/config for its own defaults — profile and config
# duplicate the same values, exactly as the compression detector does.
_DEFAULT_SESSION_SIZE_MULTIPLIERS: dict[str, float] = {
    TradingSession.ASIAN.value: 0.5,
    TradingSession.LONDON.value: 1.2,
    TradingSession.NY.value: 1.0,
    TradingSession.LONDON_NY_OVERLAP.value: 1.3,
}

_DEFAULT_SESSION_ZONE_WEIGHTS: dict[str, float] = {
    TradingSession.ASIAN.value: 0.8,
    TradingSession.LONDON.value: 1.1,
    TradingSession.NY.value: 1.0,
    TradingSession.LONDON_NY_OVERLAP.value: 1.2,
}


@dataclass(frozen=True)
class SessionWindows:
    """UTC session boundaries as hour-of-day integers (0–24).

    These are config fields, NOT magic numbers scattered through the classifier
    — override any of them to re-shape the sessions for a different market
    clock. The overlap window is expected to sit inside the NY window; a window
    whose ``start`` is greater than its ``end`` (the Asian default 21→07) wraps
    past midnight.
    """

    asian_start: int = 21
    asian_end: int = 7
    london_start: int = 7
    london_end: int = 12
    ny_start: int = 12
    ny_end: int = 21
    overlap_start: int = 12
    overlap_end: int = 16


def _in_window(hour: float, start: float, end: float) -> bool:
    """True when ``hour`` (fractional UTC hour) is within ``[start, end)``.

    Handles windows that wrap past midnight (``start > end``, e.g. the Asian
    21:00→07:00 window).
    """
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


class SessionContext:
    """Stateless UTC-time → :class:`TradingSession` classifier + tuning reader.

    Construct once at startup and share across threads. ``get_session`` is a
    pure function of the supplied (or current) UTC time and the immutable
    :class:`SessionWindows`; ``get_session_multiplier`` /
    ``get_session_zone_weight`` resolve per-instrument tuning from the
    ``InstrumentProfile`` first, then the ``EntryConfig`` default, then the
    module defaults — so a profile can override the global behaviour per symbol.
    """

    def __init__(
        self,
        windows: Optional[SessionWindows] = None,
        entry_config: Optional[EntryConfig] = None,
        profile_lookup: Optional[Callable[[str], Any]] = get_profile,
    ) -> None:
        self._windows = windows or SessionWindows()
        self._entry_config = entry_config or EntryConfig()
        self._profile_lookup = profile_lookup

    @property
    def windows(self) -> SessionWindows:
        return self._windows

    # ── Classification (pure function of UTC time + config) ───────────
    def get_session(self, utc_now: Optional[datetime] = None) -> TradingSession:
        """Return the :class:`TradingSession` active at ``utc_now``.

        ``utc_now`` defaults to the current UTC time. A naive datetime is
        assumed to already be in UTC. The overlap window is tested first so the
        most-liquid session wins when windows overlap (the overlap sits inside
        NY); the Asian window is the wrap-around default for any hour not
        covered by London / NY / overlap.
        """
        if utc_now is None:
            utc_now = datetime.now(timezone.utc)
        elif utc_now.tzinfo is not None:
            utc_now = utc_now.astimezone(timezone.utc)

        hour = utc_now.hour + utc_now.minute / 60.0
        w = self._windows

        if _in_window(hour, w.overlap_start, w.overlap_end):
            return TradingSession.LONDON_NY_OVERLAP
        if _in_window(hour, w.london_start, w.london_end):
            return TradingSession.LONDON
        if _in_window(hour, w.ny_start, w.ny_end):
            return TradingSession.NY
        if _in_window(hour, w.asian_start, w.asian_end):
            return TradingSession.ASIAN
        # Defensive fallback: any hour left uncovered by a (re-tuned) window set
        # is treated as ASIAN — the low-liquidity default.
        return TradingSession.ASIAN

    # ── Tuning resolution (profile → EntryConfig → module default) ────
    def _resolve_table(
        self, symbol: str, name: str, default: dict[str, float],
    ) -> dict[str, float]:
        """Return the tuning table for ``name``: profile first, then
        EntryConfig, then the module default. An empty/absent table at one
        level falls through to the next."""
        if self._profile_lookup is not None:
            try:
                prof = self._profile_lookup(symbol)
            except Exception:  # noqa: BLE001 — tuning lookup must never break analysis
                prof = None
            if prof is not None:
                val = getattr(prof, name, None)
                if val:
                    return val
        val = getattr(self._entry_config, name, None)
        return val if val else default

    @staticmethod
    def _session_key(session: Any) -> str:
        if isinstance(session, TradingSession):
            return session.value
        return str(getattr(session, "value", session)).upper()

    def get_session_multiplier(
        self, symbol: str, session: Any,
    ) -> float:
        """Position-size multiplier for ``symbol`` during ``session``.

        Reads ``session_size_multipliers`` from the active
        :class:`~brain.instrument_profile.InstrumentProfile`, falling back to
        the ``EntryConfig`` default and finally the module default. Returns
        ``1.0`` (neutral) for an unknown session key or on any read failure so a
        missing entry never scales a trade to zero.
        """
        key = self._session_key(session)
        try:
            table = self._resolve_table(
                symbol, "session_size_multipliers",
                _DEFAULT_SESSION_SIZE_MULTIPLIERS,
            )
            return float(table.get(key, 1.0))
        except Exception:  # noqa: BLE001 — tuning read must never break analysis
            return 1.0

    def get_session_zone_weight(
        self, symbol: str, session: Any,
    ) -> float:
        """Zone-conviction weight for ``symbol`` during ``session``.

        A multiplier applied to a zone's conviction so that setups appearing in
        a high-liquidity session (London / overlap) carry more weight than the
        same geometry in the Asian chop. Same resolution order and neutral
        fallback as :meth:`get_session_multiplier`.
        """
        key = self._session_key(session)
        try:
            table = self._resolve_table(
                symbol, "session_zone_weights",
                _DEFAULT_SESSION_ZONE_WEIGHTS,
            )
            return float(table.get(key, 1.0))
        except Exception:  # noqa: BLE001 — tuning read must never break analysis
            return 1.0

    def describe(
        self, symbol: str, utc_now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Observability snapshot: the current session plus this symbol's
        resolved size multiplier and zone weight. Never raises."""
        session = self.get_session(utc_now)
        try:
            return {
                "session": session.value,
                "size_multiplier": self.get_session_multiplier(symbol, session),
                "zone_weight": self.get_session_zone_weight(symbol, session),
            }
        except Exception:  # noqa: BLE001
            logger.debug("[session-ctx] describe failed for {}", symbol)
            return {"session": session.value, "size_multiplier": 1.0, "zone_weight": 1.0}
