"""
APEX RL — Multi-Timeframe Observation Builder
===============================================
Produces a ``(50, 48)`` observation by concatenating four timeframes
``[M5 | M15 | H1 | H4]`` of 12 market features each.

Two entry points with identical output (train/serve parity):

*  ``from_frames(m5, m15, h1, h4, instrument)``
     — batch/backtest path; takes four DataFrames.

*  ``update(tf, bar)`` + ``build(instrument, in_trade)``
     — live incremental path; one bar at a time.

**Closed-bar alignment rule** (no-leakage invariant):
    At M5 bar with timestamp *t*, the H4/H1/M15 data used is the
    last fully-closed higher-TF bar whose ``close_time <= t``.
    An in-progress higher-TF bar is **never** visible.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Optional

from .obs_builder import ObservationBuilder
from .contracts import (
    TF_ORDER,
    N_TF,
    WINDOW,
    N_MARKET_FEATURES,
    N_CONTEXT_FEATURES,
    OBS_FEATURES,
    ATR_PERIOD,
    CATEGORY_MAP,
    build_symbol_vocab,
)


class MultiTFObservationBuilder:
    """Builds ``(WINDOW, OBS_FEATURES)`` observations from four timeframes."""

    def __init__(self):
        self._tf_builders: dict[str, ObservationBuilder] = {
            tf: ObservationBuilder(window=WINDOW, atr_period=ATR_PERIOD)
            for tf in TF_ORDER
        }
        self._symbol_vocab: list[str] = build_symbol_vocab()

    # ── Batch path (training / backtest) ──────────────────────────────────

    def from_frames(
        self,
        m5_df: pd.DataFrame,
        m15_df: pd.DataFrame,
        h1_df: pd.DataFrame,
        h4_df: pd.DataFrame,
        instrument: str,
        in_trade: float = 0.0,
    ) -> tuple[Optional[np.ndarray], Optional[np.ndarray], int]:
        """
        Build one observation from four aligned DataFrames.

        Each DataFrame must have columns: time, open, high, low, close
        (volume optional).  Higher-TF frames are filtered to closed bars
        whose ``time <= last M5 bar time`` (the no-leakage invariant).

        Returns
        -------
        obs : (50, 48) float32 or None if not enough data.
        context : (N_CONTEXT_FEATURES,) float32.
        symbol_id : int index into the symbol vocabulary.
        """
        if len(m5_df) == 0:
            return None, None, 0

        current_time = m5_df["time"].iloc[-1]

        tf_frames = {
            "M5": m5_df,
            "M15": m15_df[m15_df["time"] <= current_time],
            "H1": h1_df[h1_df["time"] <= current_time],
            "H4": h4_df[h4_df["time"] <= current_time],
        }

        per_tf_obs: list[np.ndarray] = []
        for tf in TF_ORDER:
            builder = ObservationBuilder(window=WINDOW, atr_period=ATR_PERIOD)
            obs_tf = builder.from_dataframe(tf_frames[tf])
            if obs_tf is None:
                return None, None, 0
            if obs_tf.shape[-1] == N_MARKET_FEATURES:
                obs_tf[:, -1] = np.clip(float(in_trade), -3.0, 3.0)
            per_tf_obs.append(obs_tf)

        obs = np.concatenate(per_tf_obs, axis=1).astype(np.float32)
        context, symbol_id = self._build_context(instrument)
        return obs, context, symbol_id

    # ── Live incremental path ─────────────────────────────────────────────

    def update(
        self,
        tf: str,
        open: float,
        high: float,
        low: float,
        close: float,
        volume: float = 1.0,
    ) -> None:
        """Feed one closed bar for the given timeframe."""
        if tf not in self._tf_builders:
            return
        self._tf_builders[tf].update(
            open=open, high=high, low=low,
            close=close, volume=volume, in_trade=0.0,
        )

    def build(
        self,
        instrument: str,
        in_trade: float = 0.0,
    ) -> tuple[Optional[np.ndarray], Optional[np.ndarray], int]:
        """
        Assemble the current multi-TF observation from buffered bars.

        Returns ``(obs, context, symbol_id)`` — same contract as ``from_frames``.
        """
        per_tf_obs: list[np.ndarray] = []
        for tf in TF_ORDER:
            obs_tf = self._tf_builders[tf]._build(in_trade)
            if obs_tf is None:
                return None, None, 0
            per_tf_obs.append(obs_tf)

        obs = np.concatenate(per_tf_obs, axis=1).astype(np.float32)
        context, symbol_id = self._build_context(instrument)
        return obs, context, symbol_id

    # ── Context vector ────────────────────────────────────────────────────

    def _build_context(self, instrument: str) -> tuple[np.ndarray, int]:
        """
        Build the instrument-context vector and symbol id.

        Returns ``(context_vec, symbol_id)`` where ``context_vec`` has shape
        ``(N_CONTEXT_FEATURES,)`` and ``symbol_id`` is an integer index.
        """
        try:
            from config import INSTRUMENT_REGISTRY, is_session_gated, is_always_open
        except ImportError:
            return np.zeros(N_CONTEXT_FEATURES, dtype=np.float32), 0

        sym = instrument.upper()
        info = INSTRUMENT_REGISTRY.get(sym)
        if info is None:
            return np.zeros(N_CONTEXT_FEATURES, dtype=np.float32), 0

        symbol_id = self._symbol_vocab.index(sym) if sym in self._symbol_vocab else 0

        context = np.array([
            float(symbol_id),
            float(CATEGORY_MAP.get(info.category.value, 0)),
            float(np.log10(max(info.pip_size, 1e-10))),
            float(np.log10(info.typical_spread_pips + 1)),
            float(np.log10(max(info.pip_value_per_lot, 1e-10))),
            float(is_session_gated(sym)),
            float(is_always_open(sym)),
            0.0,
        ], dtype=np.float32)
        return context, symbol_id
