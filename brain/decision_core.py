"""APEX TRADER — Shared ED Decision Core.

The single implementation of the event-driven analysis pipeline, factored out of
``scanner.candle_close_handler`` so that **both** the live handler and the
backtest engine decide through identical code (no live/backtest drift).

It is pure over candle data — no EventBus, no WorldModelStore, no threads:

  * :func:`run_tf_modules`  — run the brain modules for one timeframe (the
    non-directional structural FACTS: FVG/OB/liquidity/volume/wyckoff/structure
    + concepts/regime).

The live ``CandleCloseHandler`` and developing-analysis loop delegate their
per-candle fact extraction to :func:`run_tf_modules`; the Cognitive Brain is
the sole authority that interprets those facts and forms direction.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from loguru import logger

from brain.fvg_detector import FVGDetector
from brain.inducement_detector import InducementDetector
from brain.instrument_profile import get_profile
from brain.liquidity_mapper import LiquidityMapper
from brain.market_data_utils import drop_forming_bar
from brain.order_block import OrderBlockDetector
from brain.structure_engine import StructureEngine
from brain.volume_analyzer import VolumeAnalyzer
from brain.wyckoff_engine import WyckoffEngine
from brain.concept_modules import run_concepts
from config import get_pip_size


# ── Throttled failure visibility ─────────────────────────────────────────
# Consensus vote modules run per candle-close for every symbol/TF.  A module
# that fails must be *visible* (audit #18 — no silent ``except: pass``), but a
# per-cycle WARNING would flood the log.  Log the first failure of each
# (key, module) at WARNING with the exception, then throttle repeats to at
# most once per interval so a persistently-broken module stays surfaced
# without spamming.
import time as _time

_WARN_THROTTLE_S = 300.0
_last_warned: dict[str, float] = {}


def _warn_module_failure(scope: str, module: str, symbol: str, exc: Exception) -> None:
    key = f"{scope}:{module}:{symbol}"
    now = _time.monotonic()
    last = _last_warned.get(key, 0.0)
    if now - last >= _WARN_THROTTLE_S:
        _last_warned[key] = now
        logger.warning(
            "[consensus] {} module '{}' failed for {}: {} — {}",
            scope, module, symbol, type(exc).__name__, exc,
        )


# TF → brain modules to run when that timeframe closes.  Canonical home; the
# handler re-exports this so existing imports keep working.
TF_MODULE_MAP: dict[str, list[str]] = {
    "M5": ["fvg", "order_block", "volume", "inducement", "structure"],
    "M15": ["fvg", "structure"],
    "H1": ["fvg", "order_block", "liquidity", "volume", "wyckoff", "structure"],
    "H4": ["order_block", "liquidity", "structure"],
    "D1": ["structure"],
}

# Lowest → highest timeframe, used to pick the freshest current price.
_TF_BY_RECENCY = ("M5", "M15", "H1", "H4", "D1")


# Timeframe modules publish observations, not a weighted directional bias.

def run_tf_modules(
    symbol: str,
    tf: str,
    df: pd.DataFrame,
    *,
    structure: StructureEngine,
    liquidity: LiquidityMapper,
    volume: VolumeAnalyzer,
    include_forming: bool = False,
) -> dict[str, Any]:
    """Run the brain modules for one timeframe, plus the concept generators.

    Mirrors the live handler's per-timeframe analysis exactly; the shared
    ``structure``/``liquidity``/``volume`` engines are injected so callers can
    reuse instances.  Each module is guarded so one failure can't suppress the
    rest, and a concept failure can never break the ICT analysis.

    ``include_forming`` controls the closed-bar contract.  The confirmed
    analysis path (default ``False``) drops the still-forming bar so structural
    artifacts come from CLOSED candles only.  The Phase-2 developing analysis
    passes ``True`` to treat the forming bar as if it had just closed — "if the
    candle closed right now, what would the structure look like?" — producing a
    provisional, repaint-prone view kept strictly separate from confirmed data.
    """
    modules = TF_MODULE_MAP.get(tf, [])
    if not modules:
        return {}

    try:
        pip_size = get_pip_size(symbol)
    except KeyError as exc:
        logger.error(
            "[decision-core] no registered pip_size for {} — refusing to run "
            "tf modules with a wrong default (would corrupt geometry): {}",
            symbol, exc,
        )
        raise
    profile = get_profile(symbol)
    results: dict[str, Any] = {}

    # Closed-bar contract (brain/market_data_utils.py): modules that emit
    # confirmed structural artifacts (entry zones, traps, liquidity pools) must
    # reason over CLOSED candles only, never the still-forming bar. Route those
    # to ``closed_df``. Observational reads (live momentum/regime, accumulated
    # volume) and the self-dropping StructureEngine keep the live ``df``.
    #
    # When ``include_forming`` is set (Phase-2 developing analysis), the forming
    # bar is treated as closed: ``closed_df`` IS the full frame, so the modules
    # analyze the in-progress candle as completed data. This is intentionally
    # provisional and lives only in the separate developing WorldModel store.
    closed_df = df if include_forming else drop_forming_bar(df)

    for mod in modules:
        try:
            if mod == "fvg":
                det = FVGDetector(
                    pip_size=pip_size,
                    proximity_pips=profile.fvg_proximity_pips,
                    min_size_pips=profile.fvg_min_size_pips,
                )
                results["fvg"] = det.detect(closed_df, timeframe=tf)
            elif mod == "order_block":
                det = OrderBlockDetector(
                    pip_size=pip_size,
                    min_impulse_pips=profile.ob_min_impulse_pips,
                    buffer_pips=profile.ob_buffer_pips,
                )
                results["order_block"] = det.detect(closed_df, timeframe=tf)
            elif mod == "liquidity":
                results["liquidity"] = liquidity.map(closed_df, pip_size)
            elif mod == "volume":
                # Live ``df`` drives observational reads (developing volume);
                # ``closed_df`` drives structural verdicts (spike/climax/bias).
                results["volume"] = volume.analyze(df, closed_df=closed_df)
            elif mod == "wyckoff":
                if profile.wyckoff_enabled:
                    wyck = WyckoffEngine(
                        pip_size=pip_size,
                        swing_lookback=profile.swing_lookback,
                    )
                    # WyckoffEngine self-drops the forming bar for its pattern
                    # reads and feeds its inner StructureEngine the live frame
                    # (which self-drops), so it receives the live ``df`` here to
                    # avoid a double-drop.
                    results["wyckoff"] = wyck.analyze(df)
            elif mod == "inducement":
                det = InducementDetector(pip_size=pip_size)
                results["inducement"] = det.analyze(closed_df)
            elif mod == "structure":
                results["structure"] = structure.analyze(df, pip_size=pip_size, context=f"{symbol}/{tf}")
        except Exception as exc:
            _warn_module_failure(f"tf-{tf}", mod, symbol, exc)

    try:
        signals, regime = run_concepts(df)
        results["concepts"] = signals
        results["regime"] = regime
    except Exception as exc:
        _warn_module_failure(f"tf-{tf}", "concepts", symbol, exc)

    return results


