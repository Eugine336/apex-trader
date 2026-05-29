"""
APEX TRADER — Broker-Agnostic Symbol Mapper
Translates canonical APEX symbols to broker-specific names and back.
Config is driven by JSON files under config/brokers/ so adding a new
broker never requires touching Python code.
"""

import json
from pathlib import Path
from typing import Optional

from loguru import logger

from config import INSTRUMENT_REGISTRY, InstrumentCategory

_BROKERS_DIR = Path(__file__).parent.parent / "config" / "brokers"


class SymbolMapper:
    """
    Two-way symbol translator between APEX canonical symbols and a
    specific broker's naming convention.

    Resolution order (highest priority first):
      1. Overrides  — explicit symbol-by-symbol map from the JSON config
      2. Rules      — category-based pattern (e.g. all forex → frx{symbol})
      3. Passthrough — return the canonical symbol unchanged
    """

    def __init__(self, broker: str) -> None:
        self._broker = broker.lower()
        self._rules: dict[str, str] = {}      # category → format pattern
        self._overrides: dict[str, str] = {}  # canonical (UPPER) → broker
        self._reverse: dict[str, str] = {}    # broker → canonical (UPPER)
        self._load_config()

    # ── Config loading ────────────────────────────────────────────────────

    def _load_config(self) -> None:
        config_path = _BROKERS_DIR / f"{self._broker}.json"
        if not config_path.exists():
            logger.warning(
                "SymbolMapper: no config for broker '{}', using passthrough",
                self._broker,
            )
            return
        try:
            with open(config_path) as fh:
                cfg = json.load(fh)
            for cat_name, rule in cfg.get("rules", {}).items():
                self._rules[cat_name.lower()] = rule["pattern"]
            for canonical, broker_sym in cfg.get("overrides", {}).items():
                self._overrides[canonical.upper()] = broker_sym
                self._reverse[broker_sym] = canonical.upper()
        except Exception as exc:
            logger.error(
                "SymbolMapper: failed to load config for '{}': {}", self._broker, exc
            )

    # ── Public API ────────────────────────────────────────────────────────

    def to_broker(self, canonical: str) -> str:
        """Translate an APEX canonical symbol to the broker-specific name."""
        canonical = canonical.upper()
        if canonical in self._overrides:
            return self._overrides[canonical]
        info = INSTRUMENT_REGISTRY.get(canonical)
        if info:
            pattern = self._rules.get(info.category.value)
            if pattern:
                broker_sym = pattern.replace("{symbol}", canonical)
                self._reverse.setdefault(broker_sym, canonical)
                return broker_sym
        return canonical

    def to_canonical(self, broker_sym: str) -> str:
        """Translate a broker-specific symbol back to the APEX canonical name."""
        return self._reverse.get(broker_sym, broker_sym)

    @property
    def broker(self) -> str:
        return self._broker
