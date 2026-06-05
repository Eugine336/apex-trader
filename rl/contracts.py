"""
APEX RL — Observation Contract
===============================
Single source of truth for the multi-timeframe observation schema.
Every component that produces or consumes RL observations references this.

Checkpoint metadata MUST embed ``schema_hash()`` at save time.
``assert_compatible()`` blocks loading a checkpoint whose schema diverges.
"""

from __future__ import annotations

import hashlib
import json

OBS_CONTRACT_VERSION = "mtf-v1"

TF_ORDER = ["M5", "M15", "H1", "H4"]
N_TF = len(TF_ORDER)
WINDOW = 50

MARKET_FEATURES = [
    "o_n", "h_n", "l_n", "c_n", "v_n", "atr_n",
    "ret1", "ret5", "ret14",
    "hl_ratio", "oc_ratio", "in_trade",
]
N_MARKET_FEATURES = len(MARKET_FEATURES)  # 12

OBS_FEATURES = N_TF * N_MARKET_FEATURES   # 48
OBS_SHAPE = (WINDOW, OBS_FEATURES)         # (50, 48)

INSTRUMENT_CONTEXT_FEATURES = [
    "symbol_id",
    "category_id",
    "pip_size_log",
    "typical_spread_log",
    "pip_value_log",
    "is_session_gated",
    "is_always_open",
    "volatility_class",
]
N_CONTEXT_FEATURES = len(INSTRUMENT_CONTEXT_FEATURES)  # 8

ATR_PERIOD = 14

CATEGORY_MAP = {
    "forex": 0,
    "commodity": 1,
    "index": 2,
    "synthetic": 3,
    "crypto": 4,
}


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


def schema_hash() -> str:
    """Deterministic hash of the observation schema for checkpoint gating."""
    spec = json.dumps({
        "version": OBS_CONTRACT_VERSION,
        "tf_order": TF_ORDER,
        "window": WINDOW,
        "market_features": MARKET_FEATURES,
        "context_features": INSTRUMENT_CONTEXT_FEATURES,
        "obs_shape": list(OBS_SHAPE),
    }, sort_keys=True)
    return hashlib.sha256(spec.encode()).hexdigest()[:16]


def assert_compatible(checkpoint_meta: dict) -> None:
    """Raise if a checkpoint's schema doesn't match the current contract."""
    ckpt_hash = checkpoint_meta.get("schema_hash", "")
    current = schema_hash()
    if ckpt_hash != current:
        raise ValueError(
            f"Checkpoint schema mismatch: checkpoint={ckpt_hash}, "
            f"current={current}. Retrain with the current obs contract."
        )
