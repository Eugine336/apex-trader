"""APEX TRADER — LiveCandleAggregator (Phase 1 of Institutional Market Model).

Builds OHLC candles locally from the live tick stream for all supported
timeframes (M1, M5, M15, H1, H4, D1) without any broker fetch after the
initial warmup seed.

It hooks into the existing :class:`tick.tick_router.TickRouter` via
``register_callback`` and receives every *stored* tick (post-coalescing,
~15Hz max per symbol).  For each (symbol, timeframe) it maintains:

* a ring buffer (``deque`` with ``maxlen``) of **closed** candles, and
* one **forming** candle that is updated on every tick.

When a tick's timestamp crosses a candle boundary, the forming candle is
closed (pushed to the ring buffer) and a fresh forming candle is started.

This is purely a data layer.  It is **additive** — the confirmed analysis
path (``CandleCloseHandler`` → broker fetch on candle close) is untouched
and remains authoritative.  Phase 2 will read ``get_dataframe`` from this
aggregator to run continuous "developing" analysis between candle closes.

Thread-safe: ``on_tick`` may be called concurrently from multiple broker
threads (MT5 poll, Deriv WS).  A single re-entrant lock guards all state.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import pandas as pd
from loguru import logger

from tick.candle_close_detector import _TF_SECONDS, SUPPORTED_TIMEFRAMES
from tick.models import Tick

# Column order matching broker-fetched DataFrames (see mt5_connector.get_ohlcv).
_COLUMNS = ["time", "open", "high", "low", "close", "volume"]


def _candle_boundary(epoch: float, tf_seconds: int) -> float:
    """Return the epoch of the candle boundary that ``epoch`` belongs to.

    Mirrors :func:`tick.candle_close_detector._candle_boundary` — kept local
    so the aggregator does not couple to another module's internal helper.
    """
    return float((int(epoch) // tf_seconds) * tf_seconds)


class LiveCandleAggregator:
    """Builds OHLC candles from the live tick stream for all timeframes.

    Parameters
    ----------
    max_candles : int
        Ring buffer capacity (number of closed candles retained) per
        (symbol, timeframe).  Default 200 to match the broker fetch depth.
    """

    def __init__(self, max_candles: int = 200) -> None:
        self._max_candles = max(1, int(max_candles))
        self._lock = threading.RLock()
        # (symbol, tf) -> list[dict] of closed candles (kept bounded manually
        # so we can slice/copy without deque-index overhead).
        self._closed: dict[tuple[str, str], list[dict]] = {}
        # (symbol, tf) -> forming candle dict (boundary, open, high, low,
        # close, tick_count).
        self._forming: dict[tuple[str, str], dict] = {}
        # Observability counters.
        self._candles_closed: int = 0
        self._warmup_pairs_seeded: int = 0

    # ------------------------------------------------------------------
    # Tick ingestion (hot path)
    # ------------------------------------------------------------------

    def on_tick(self, tick: Tick) -> None:
        """Update forming candles for all timeframes from a single tick.

        Registered with ``TickRouter.register_callback``.  Best-effort and
        fully self-contained: any failure is caught and logged so it can
        never propagate back into the tick router (which would break tick
        routing for every other subscriber).
        """
        try:
            symbol = tick.symbol
            epoch = tick.epoch
            mid = tick.mid
            with self._lock:
                for tf, tf_secs in _TF_SECONDS.items():
                    key = (symbol, tf)
                    boundary = _candle_boundary(epoch, tf_secs)
                    forming = self._forming.get(key)

                    if forming is None:
                        self._forming[key] = self._new_forming(boundary, mid)
                        continue

                    if boundary != forming["boundary"]:
                        # Boundary crossed — close the forming candle. A gap
                        # spanning multiple periods (weekend / illiquid pause)
                        # closes exactly one candle and starts fresh at the new
                        # boundary; no empty intermediate candles are fabricated
                        # (matches the tick-path behaviour of CandleCloseDetector).
                        self._close_forming(key, forming)
                        self._forming[key] = self._new_forming(boundary, mid)
                    else:
                        # Same period — update high / low / close.
                        if mid > forming["high"]:
                            forming["high"] = mid
                        if mid < forming["low"]:
                            forming["low"] = mid
                        forming["close"] = mid
                        forming["tick_count"] += 1
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "[live-candles] on_tick error: {} — {}", type(exc).__name__, exc
            )

    @staticmethod
    def _new_forming(boundary: float, mid: float) -> dict:
        return {
            "boundary": boundary,
            "open": mid,
            "high": mid,
            "low": mid,
            "close": mid,
            "tick_count": 1,
        }

    def _close_forming(self, key: tuple[str, str], forming: dict) -> None:
        """Push a finished forming candle into the ring buffer (lock held)."""
        buf = self._closed.get(key)
        if buf is None:
            buf = []
            self._closed[key] = buf
        buf.append(
            {
                "boundary": forming["boundary"],
                "open": forming["open"],
                "high": forming["high"],
                "low": forming["low"],
                "close": forming["close"],
                "volume": float(forming["tick_count"]),
            }
        )
        # Bound the ring buffer.
        if len(buf) > self._max_candles:
            del buf[: len(buf) - self._max_candles]
        self._candles_closed += 1

    # ------------------------------------------------------------------
    # Warmup (seed from broker history)
    # ------------------------------------------------------------------

    def warmup(
        self,
        symbols: list[str],
        timeframes: list[str],
        candle_fetcher: Callable[[str, str, int], Optional[pd.DataFrame]],
        per_symbol_timeout: float = 30.0,
    ) -> int:
        """Seed the ring buffers from broker history before live ticks start.

        For each (symbol, timeframe) it fetches up to ``max_candles`` bars via
        ``candle_fetcher`` and stores **all** returned rows as closed candles —
        at warmup time every fetched bar (including the broker's in-progress
        last bar) is treated as closed.  The first live tick then starts a
        fresh forming candle.

        Must be called BEFORE the live tick loops start so it cannot race a
        live tick for the same (symbol, timeframe).  Best-effort: any fetch or
        parse failure is logged and skipped.

        Parameters
        ----------
        symbols : list[str]
            Instruments to seed.
        timeframes : list[str]
            Timeframes to seed (only supported ones are used).
        candle_fetcher : callable
            ``(symbol, timeframe, count) -> DataFrame | None`` with columns
            ``time, open, high, low, close, volume`` (broker format).
        per_symbol_timeout : float
            Max seconds to wait for a single symbol's seed to complete.

        Returns
        -------
        int
            Number of (symbol, timeframe) pairs successfully seeded.
        """
        tfs = [tf for tf in timeframes if tf in _TF_SECONDS]
        if not symbols or not tfs:
            return 0

        pool = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="live-candles-warmup"
        )
        try:
            futures = {
                pool.submit(self._warmup_symbol, sym, tfs, candle_fetcher): sym
                for sym in symbols
            }
            seeded = 0
            for fut, sym in futures.items():
                try:
                    seeded += fut.result(timeout=per_symbol_timeout)
                except Exception as exc:
                    logger.debug(
                        "[live-candles] warmup failed for {}: {}", sym, exc
                    )
        finally:
            pool.shutdown(wait=False)

        with self._lock:
            self._warmup_pairs_seeded += seeded
        logger.info(
            "[live-candles] warmup complete — seeded {} (symbol, timeframe) "
            "pair(s) across {} symbol(s) × {} timeframe(s)",
            seeded, len(symbols), len(tfs),
        )
        return seeded

    def _warmup_symbol(
        self,
        symbol: str,
        timeframes: list[str],
        candle_fetcher: Callable[[str, str, int], Optional[pd.DataFrame]],
    ) -> int:
        n = 0
        for tf in timeframes:
            try:
                if self._warmup_one(symbol, tf, candle_fetcher):
                    n += 1
            except Exception as exc:
                logger.debug(
                    "[live-candles] warmup {} {} failed: {}", symbol, tf, exc
                )
        return n

    def _warmup_one(
        self,
        symbol: str,
        tf: str,
        candle_fetcher: Callable[[str, str, int], Optional[pd.DataFrame]],
    ) -> bool:
        df = candle_fetcher(symbol, tf, self._max_candles)
        if df is None or getattr(df, "empty", True):
            return False
        tf_secs = _TF_SECONDS[tf]
        seeded_rows: list[dict] = []
        for row in df.itertuples(index=False):
            try:
                row_epoch = pd.Timestamp(row.time).timestamp()
            except Exception:
                continue
            seeded_rows.append(
                {
                    "boundary": _candle_boundary(row_epoch, tf_secs),
                    "open": float(row.open),
                    "high": float(row.high),
                    "low": float(row.low),
                    "close": float(row.close),
                    "volume": float(getattr(row, "volume", 0.0) or 0.0),
                }
            )
        if not seeded_rows:
            return False
        # Keep only the most recent ``max_candles`` rows.
        if len(seeded_rows) > self._max_candles:
            seeded_rows = seeded_rows[-self._max_candles:]
        with self._lock:
            self._closed[(symbol, tf)] = seeded_rows
        return True

    # ------------------------------------------------------------------
    # Read accessors
    # ------------------------------------------------------------------

    def get_dataframe(self, symbol: str, tf: str) -> Optional[pd.DataFrame]:
        """Return closed candles plus the current forming bar.

        The forming bar (if present) is the **last** row — matching the broker
        contract where ``iloc[-1]`` is the in-progress candle.  Returns ``None``
        when no data exists for this (symbol, timeframe).
        """
        with self._lock:
            rows = list(self._closed.get((symbol, tf), []))
            forming = self._forming.get((symbol, tf))
            if forming is not None:
                rows.append(
                    {
                        "boundary": forming["boundary"],
                        "open": forming["open"],
                        "high": forming["high"],
                        "low": forming["low"],
                        "close": forming["close"],
                        "volume": float(forming["tick_count"]),
                    }
                )
        if not rows:
            return None
        return self._rows_to_df(rows)

    def get_confirmed_dataframe(self, symbol: str, tf: str) -> Optional[pd.DataFrame]:
        """Return closed candles only (no forming bar).

        Returns ``None`` when no closed candles exist for this (symbol,
        timeframe).
        """
        with self._lock:
            rows = list(self._closed.get((symbol, tf), []))
        if not rows:
            return None
        return self._rows_to_df(rows)

    @staticmethod
    def _rows_to_df(rows: list[dict]) -> pd.DataFrame:
        data = {
            "time": [
                datetime.fromtimestamp(r["boundary"], tz=timezone.utc) for r in rows
            ],
            "open": [r["open"] for r in rows],
            "high": [r["high"] for r in rows],
            "low": [r["low"] for r in rows],
            "close": [r["close"] for r in rows],
            "volume": [r["volume"] for r in rows],
        }
        return pd.DataFrame(data, columns=_COLUMNS)

    # ------------------------------------------------------------------
    # Lifecycle / observability
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Reactive component — no threads. Logs for lifecycle visibility."""
        logger.info(
            "[live-candles] started (max_candles={}, timeframes={})",
            self._max_candles, ",".join(SUPPORTED_TIMEFRAMES),
        )

    def stop(self) -> None:
        logger.info(
            "[live-candles] stopped (candles_closed={}, pairs_tracked={})",
            self._candles_closed, self._pairs_tracked(),
        )

    def stats(self) -> dict[str, Any]:
        with self._lock:
            symbols = {key[0] for key in self._forming}
            symbols.update(key[0] for key in self._closed)
            return {
                "candles_closed": self._candles_closed,
                "forming_candles": len(self._forming),
                "symbols_tracked": len(symbols),
                "warmup_pairs_seeded": self._warmup_pairs_seeded,
            }

    def _pairs_tracked(self) -> int:
        with self._lock:
            return len(set(self._closed) | set(self._forming))
