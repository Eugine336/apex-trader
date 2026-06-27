"""APEX TRADER — consolidation hardening tests.

Covers the silent-failure hardening + developing-store direction guard:

  * ``_safe_pip_size`` fallback to 0.0001 now logs a WARNING (once per symbol)
    and records the symbol so the entry path can tag the trade.
  * the ``pnl_pips`` close-path calculation logs instead of silently passing.
  * the developing WorldModelStore enforces its "confidence only, never
    direction" contract at the publish boundary — a developing bias direction
    that contradicts the confirmed store is neutralised (fail-loud).
  * a scoped ratchet on bare ``except Exception: pass`` blocks in the main loop
    so new silent swallows can't be added to the money path unnoticed.
"""

from __future__ import annotations

import ast
import pathlib
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

from loguru import logger

import event_driven_bootstrap as edb
from brain.developing_analysis import DevelopingAnalysisLoop
from brain.structure_engine import StructureAnalysis, StructureEvent, Trend
from brain.world_model import WorldModelStore, build_world_model
from config import DevelopingAnalysisConfig

_BOOTSTRAP_PATH = pathlib.Path(edb.__file__)


# ── Helpers ───────────────────────────────────────────────────────────────


@contextmanager
def _capture_logs(level: str = "WARNING"):
    """Capture loguru records at/above ``level`` into a list of strings."""
    captured: list[str] = []
    sink_id = logger.add(
        lambda m: captured.append(str(m)), level=level, format="{message}",
    )
    try:
        yield captured
    finally:
        logger.remove(sink_id)


def _sa(trend: str, confidence: float) -> StructureAnalysis:
    return StructureAnalysis(
        trend=Trend(trend),
        last_event=StructureEvent.NONE,
        swing_high=None,
        swing_low=None,
        last_bos_level=None,
        last_choch_level=None,
        structure_broken=False,
        bullish_swing_points=[],
        bearish_swing_points=[],
        confidence=confidence,
    )


class _FakeAggregator:
    def get_dataframe(self, symbol, tf):
        return None


def _dev_loop(confirmed_store=None) -> DevelopingAnalysisLoop:
    return DevelopingAnalysisLoop(
        candle_aggregator=_FakeAggregator(),
        developing_store=WorldModelStore(),
        symbols=["EURUSD"],
        config=DevelopingAnalysisConfig(),
        confirmed_store=confirmed_store,
    )


def _publish_confirmed(store: WorldModelStore, symbol: str, direction: str) -> None:
    wm = build_world_model(
        symbol=symbol,
        version=store.next_version(),
        timestamp=datetime.now(timezone.utc),
        structure={"H1": _sa("BULLISH", 0.8)},
        bias={"direction": direction, "score": 80, "confidence": 0.8},
    )
    store.publish(wm)


# ── (a) pip_size fallback logs a warning ────────────────────────────────────


def test_pip_size_fallback_logs_warning(monkeypatch):
    """When get_pip_size raises, _safe_pip_size returns 0.0001 AND warns."""
    ev = edb.PositionEvaluator.__new__(edb.PositionEvaluator)
    ev._pip_size_fallback_symbols = set()
    ev._pm = SimpleNamespace()  # no get_symbol_spec → skips spec branch

    def _boom(_sym):
        raise RuntimeError("registry down")

    monkeypatch.setattr(edb, "get_pip_size", _boom)
    monkeypatch.setattr(edb, "resolve_to_internal", lambda s: s)

    with _capture_logs("WARNING") as logs:
        val = ev._safe_pip_size("V75")

    assert val == 0.0001
    assert "V75" in ev._pip_size_fallback_symbols
    assert any("fallback to 0.0001" in line for line in logs)


def test_pip_size_fallback_warns_once_per_symbol(monkeypatch):
    """Repeated fallbacks for the same symbol warn only once (no flood)."""
    ev = edb.PositionEvaluator.__new__(edb.PositionEvaluator)
    ev._pip_size_fallback_symbols = set()
    ev._pm = SimpleNamespace()

    monkeypatch.setattr(
        edb, "get_pip_size",
        lambda s: (_ for _ in ()).throw(RuntimeError("x")),
    )
    monkeypatch.setattr(edb, "resolve_to_internal", lambda s: s)

    with _capture_logs("WARNING") as logs:
        ev._safe_pip_size("BOOM1000")
        ev._safe_pip_size("BOOM1000")
        ev._safe_pip_size("BOOM1000")

    fallback_lines = [l for l in logs if "fallback to 0.0001" in l]
    assert len(fallback_lines) == 1


def test_pip_size_success_does_not_warn(monkeypatch):
    """A successful pip lookup never warns and never records a fallback."""
    ev = edb.PositionEvaluator.__new__(edb.PositionEvaluator)
    ev._pip_size_fallback_symbols = set()
    ev._pm = SimpleNamespace()

    monkeypatch.setattr(edb, "get_pip_size", lambda s: 0.01)
    monkeypatch.setattr(edb, "resolve_to_internal", lambda s: s)

    with _capture_logs("WARNING") as logs:
        val = ev._safe_pip_size("GER40")

    assert val == 0.01
    assert "GER40" not in ev._pip_size_fallback_symbols
    assert not any("fallback to 0.0001" in line for line in logs)


# ── (b) pnl_pips failure logs instead of silently passing ────────────────────


def test_pnl_pips_failure_logs_warning():
    """The close-path pnl_pips handler logs; the old silent `pass` is gone."""
    src = _BOOTSTRAP_PATH.read_text(encoding="utf-8")
    # The fix message must be present in the close path.
    assert "[close-pnl] pnl_pips calculation failed" in src
    # The exact pre-fix silent snippet must no longer exist.
    silent = (
        "pnl_pips = (entry - close_price) / pip_size\n"
        "            except Exception:\n"
        "                pass"
    )
    assert silent not in src


# ── (c)–(e) developing-store direction guard ────────────────────────────────


def test_developing_store_direction_guard(monkeypatch):
    """Developing direction contradicting confirmed is neutralised (fail-loud)."""
    confirmed = WorldModelStore()
    _publish_confirmed(confirmed, "EURUSD", "LONG")
    loop = _dev_loop(confirmed_store=confirmed)

    # Force the developing bias to SHORT (contradicts confirmed LONG).
    monkeypatch.setattr(
        "brain.developing_analysis.compute_bias",
        lambda struct: {"direction": "SHORT", "score": 80, "confidence": 0.8},
    )

    with _capture_logs("WARNING") as logs:
        loop._merge_and_publish("EURUSD", "H1", {"structure": _sa("BEARISH", 0.8)})

    published = loop._developing_store.get("EURUSD")
    assert published is not None
    assert published.bias_dict().get("direction", "") == ""
    assert published.bias_dict().get("score", 0) == 0
    assert any("developing-guard" in line for line in logs)


def test_developing_store_same_direction_passes(monkeypatch):
    """Developing direction matching confirmed is preserved (not neutralised)."""
    confirmed = WorldModelStore()
    _publish_confirmed(confirmed, "EURUSD", "LONG")
    loop = _dev_loop(confirmed_store=confirmed)

    monkeypatch.setattr(
        "brain.developing_analysis.compute_bias",
        lambda struct: {"direction": "LONG", "score": 80, "confidence": 0.8},
    )

    loop._merge_and_publish("EURUSD", "H1", {"structure": _sa("BULLISH", 0.8)})

    published = loop._developing_store.get("EURUSD")
    assert published is not None
    assert published.bias_dict().get("direction", "") == "LONG"


def test_developing_store_no_confirmed_model_passes(monkeypatch):
    """No confirmed model → developing direction passes through unchanged."""
    confirmed = WorldModelStore()  # empty — no model for EURUSD
    loop = _dev_loop(confirmed_store=confirmed)

    monkeypatch.setattr(
        "brain.developing_analysis.compute_bias",
        lambda struct: {"direction": "SHORT", "score": 80, "confidence": 0.8},
    )

    loop._merge_and_publish("EURUSD", "H1", {"structure": _sa("BEARISH", 0.8)})

    published = loop._developing_store.get("EURUSD")
    assert published is not None
    assert published.bias_dict().get("direction", "") == "SHORT"


def test_developing_store_no_confirmed_store_passes(monkeypatch):
    """confirmed_store=None disables the guard (standalone/back-compat)."""
    loop = _dev_loop(confirmed_store=None)

    monkeypatch.setattr(
        "brain.developing_analysis.compute_bias",
        lambda struct: {"direction": "SHORT", "score": 80, "confidence": 0.8},
    )

    loop._merge_and_publish("EURUSD", "H1", {"structure": _sa("BEARISH", 0.8)})

    published = loop._developing_store.get("EURUSD")
    assert published is not None
    assert published.bias_dict().get("direction", "") == "SHORT"


# ── (f) ratchet: bare `except Exception: pass` in the money path ─────────────


def test_bare_except_pass_blocks_below_threshold():
    """Scoped ratchet on bare ``except Exception: pass`` in the main loop.

    These swallow errors with neither ``as exc`` nor any logging. The repo-wide
    ``test_no_silent_exception_handlers`` is the strict goal; this scoped
    threshold prevents *new* silent swallows being added to the money-critical
    event loop while the broader cleanup proceeds.
    """
    src = _BOOTSTRAP_PATH.read_text(encoding="utf-8")
    bare = re.findall(r"except Exception:\s*\n\s*pass\b", src)
    # Current count after this hardening pass is 36; allow small headroom but
    # fail loudly if a batch of new silent swallows lands.
    assert len(bare) <= 38, (
        f"{len(bare)} bare `except Exception: pass` blocks in "
        "event_driven_bootstrap.py — add `as exc` + logging instead of "
        "silently swallowing in the money path."
    )


def test_pip_size_fallback_tag_present_in_entry_context():
    """The entry context records pip_size_fallback so attribution sees it."""
    src = _BOOTSTRAP_PATH.read_text(encoding="utf-8")
    assert '"pip_size_fallback"' in src
    assert "self._pip_size_fallback_symbols" in src


def test_module_parses():
    """Sanity: the edited bootstrap module is syntactically valid."""
    ast.parse(_BOOTSTRAP_PATH.read_text(encoding="utf-8"))
