"""
APEX TRADER — Broker-Agnostic Symbol Mapper
Translates canonical APEX symbols to broker-specific names and back.
Config is driven by JSON files under config/brokers/ so adding a new
broker never requires touching Python code.
"""

import json
from pathlib import Path

from loguru import logger

from config import INSTRUMENT_REGISTRY

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


# ---------------------------------------------------------------------------
# Module-level convenience — resolve any broker-native symbol to internal
# ---------------------------------------------------------------------------

_BROKER_CONFIGS = ("deriv", "icmarkets", "metaquotes_ltd")


def resolve_to_internal(broker_symbol: str) -> str:
    """Resolve a broker-native symbol (e.g. ``1HZ50V``) to the APEX
    internal registry key (e.g. ``V50_1S``).

    Resolution order:
      1. Already a known registry key → return as-is.
      2. Try ``SymbolMapper.to_canonical()`` for each known broker config
         (deriv, icmarkets, metaquotes_ltd). The reverse maps are built from
         ``config/brokers/<broker>.json`` — one canonical source.
      3. No match → return the input unchanged (never raises).

    The Deriv 1HZ##V ↔ V##_1S family is the primary case this handles:
    on restart the Deriv connector reports broker-native symbols, but the
    instrument registry and all downstream code expect the internal key.
    """
    normalized = broker_symbol.upper().replace("/", "")
    if normalized in INSTRUMENT_REGISTRY:
        return normalized
    for broker_name in _BROKER_CONFIGS:
        mapper = SymbolMapper(broker_name)
        for sym in INSTRUMENT_REGISTRY:
            mapper.to_broker(sym)
        canonical = mapper.to_canonical(broker_symbol)
        if canonical != broker_symbol and canonical.upper() in INSTRUMENT_REGISTRY:
            return canonical
    return broker_symbol
