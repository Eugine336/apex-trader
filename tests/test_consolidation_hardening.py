"""APEX TRADER — consolidation hardening tests.

Covers the silent-failure hardening + developing-store direction guard:

  * ``_safe_pip_size`` fallback to 0.0001 now logs a WARNING (once per symbol)
    and records the symbol so the entry path can tag the trade.
  * the ``pnl_pips`` close-path calculation logs instead of silently passing.
  * a scoped ratchet on bare ``except Exception: pass`` blocks in the main loop
    so new silent swallows can't be added to the money path unnoticed.
"""

from __future__ import annotations

import ast
import pathlib
import re
from contextlib import contextmanager
from types import SimpleNamespace

from loguru import logger

import event_driven_bootstrap as edb

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
