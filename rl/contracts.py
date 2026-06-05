"""
APEX RL — Multi-Timeframe Observation Contract
================================================
Single source of truth for the MTF observation format shared by
the training environment, the live observation builder, and the
bridge checkpoint loader.

Version: mtf-v1
Shape  : (50, 48)  — 50 timesteps × (12 features × 4 timeframes)
Context: float32 vector of instrument profile features
"""

from __future__ import annotations

import hashlib
import json

# ── Observation window ────────────────────────────────────────────────────────

OBS_CONTRACT_VERSION = "mtf-v1"

WINDOW = 50
ATR_PERIOD = 14
N_MARKET_FEATURES = 12

MARKET_FEATURES = [
    "o_n",
    "h_n",
    "l_n",
    "c_n",
    "v_n",
    "atr_n",
    "ret1",
    "ret5",
    "ret14",
    "hl_ratio",
    "oc_ratio",
    "in_trade",
]

assert len(MARKET_FEATURES) == N_MARKET_FEATURES

# ── Timeframe ordering ────────────────────────────────────────────────────────

TF_ORDER = ["M5", "M15", "H1", "H4"]
CLOCK_TF = "M5"

TF_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "H1": 3600,
    "H4": 14400,
}

N_TIMEFRAMES = len(TF_ORDER)
OBS_SHAPE = (WINDOW, N_MARKET_FEATURES * N_TIMEFRAMES)  # (50, 48)

# ── Alignment rule ────────────────────────────────────────────────────────────
# A higher-TF bar with open time T is *usable* at an M5 anchor bar with
# open time t **iff** T + TF_SECONDS[tf] <= t  (the bar is fully closed).
# For the clock TF (M5) itself the anchor bar IS included (T <= t).
# This prevents look-ahead from in-progress higher-TF bars.

ALIGNMENT_RULE = (
    "A higher-TF bar with open_time T is usable at M5 anchor open_time t "
    "iff T + TF_SECONDS[tf] <= t  (last fully CLOSED bar; never the "
    "in-progress bar). For the clock TF (M5), the anchor bar itself is "
    "included (T <= t)."
)

# ── Instrument context ────────────────────────────────────────────────────────

INSTRUMENT_CONTEXT_FEATURES = [
    "log_pip_size",
    "typical_spread_pips",
    "log_pip_value",
    "is_jpy_pair",
    "is_metal",
    "is_index",
    "is_crypto",
    "is_always_open",
]

N_CONTEXT_FEATURES = len(INSTRUMENT_CONTEXT_FEATURES)

# ── Schema hashing ────────────────────────────────────────────────────────────


def schema() -> dict:
    """Canonical, JSON-serialisable representation of the contract."""
    return {
        "version": OBS_CONTRACT_VERSION,
        "window": WINDOW,
        "atr_period": ATR_PERIOD,
        "n_market_features": N_MARKET_FEATURES,
        "market_features": MARKET_FEATURES,
        "tf_order": TF_ORDER,
        "clock_tf": CLOCK_TF,
        "obs_shape": list(OBS_SHAPE),
        "n_context_features": N_CONTEXT_FEATURES,
        "context_features": INSTRUMENT_CONTEXT_FEATURES,
    }


def schema_hash() -> str:
    """First 16 hex chars of SHA-256 over the canonical JSON schema."""
    blob = json.dumps(schema(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def assert_compatible(checkpoint_meta: dict) -> None:
    """Raise ``ValueError`` if *checkpoint_meta* is incompatible."""
    expected_version = OBS_CONTRACT_VERSION
    expected_hash = schema_hash()

    ckpt_version = checkpoint_meta.get("obs_contract_version")
    ckpt_hash = checkpoint_meta.get("obs_schema_hash")

    if ckpt_version != expected_version:
        raise ValueError(
            f"Checkpoint obs_contract_version={ckpt_version!r} != "
            f"expected {expected_version!r}"
        )
    if ckpt_hash != expected_hash:
        raise ValueError(
            f"Checkpoint obs_schema_hash={ckpt_hash!r} != "
            f"expected {expected_hash!r}"
        )


# ── Phase 2 additions ────────────────────────────────────────────────────────


def build_symbol_vocab() -> list[str]:
    """Sorted list of trainable symbols (excludes synthetics — no data)."""
    try:
        from config import INSTRUMENT_REGISTRY, InstrumentCategory
        return sorted(
            sym for sym, info in INSTRUMENT_REGISTRY.items()
            if info.category != InstrumentCategory.SYNTHETIC
        )
    except ImportError:
        return []
