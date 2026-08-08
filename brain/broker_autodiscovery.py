"""
APEX TRADER — Broker Auto-Discovery
Runs on startup. Connects to MT5, scans ALL broker symbols,
fuzzy-matches them to APEX canonical names, and writes the
broker JSON automatically.

You never touch a JSON file again — even if you switch brokers.
Add 20 brokers, same story. It figures itself out.
"""

import json
import re
from pathlib import Path
from typing import Optional
from loguru import logger

_BROKERS_DIR = Path(__file__).parent.parent / "config" / "brokers"

# ─────────────────────────────────────────────────────────────────────────────
# The knowledge base — every known alias for every APEX canonical symbol.
# The more aliases here, the smarter the auto-match.
# ─────────────────────────────────────────────────────────────────────────────
APEX_ALIASES: dict[str, list[str]] = {
    # Forex — most brokers pass these through unchanged
    "EURUSD":  ["EURUSD", "EUR/USD", "EUR_USD"],
    "GBPUSD":  ["GBPUSD", "GBP/USD", "GBP_USD"],
    "USDJPY":  ["USDJPY", "USD/JPY", "USD_JPY"],
    "USDCHF":  ["USDCHF", "USD/CHF", "USD_CHF"],
    "AUDUSD":  ["AUDUSD", "AUD/USD", "AUD_USD"],
    "NZDUSD":  ["NZDUSD", "NZD/USD", "NZD_USD"],
    "USDCAD":  ["USDCAD", "USD/CAD", "USD_CAD"],
    "EURGBP":  ["EURGBP", "EUR/GBP"],
    "EURJPY":  ["EURJPY", "EUR/JPY"],
    "GBPJPY":  ["GBPJPY", "GBP/JPY"],
    "AUDJPY":  ["AUDJPY", "AUD/JPY"],
    "CADJPY":  ["CADJPY", "CAD/JPY"],
    "CHFJPY":  ["CHFJPY", "CHF/JPY"],
    "NZDJPY":  ["NZDJPY", "NZD/JPY"],
    "EURCHF":  ["EURCHF", "EUR/CHF"],
    "EURAUD":  ["EURAUD", "EUR/AUD"],
    "EURCAD":  ["EURCAD", "EUR/CAD"],
    "EURNZD":  ["EURNZD", "EUR/NZD"],
    "GBPAUD":  ["GBPAUD", "GBP/AUD"],
    "GBPCAD":  ["GBPCAD", "GBP/CAD"],
    "GBPCHF":  ["GBPCHF", "GBP/CHF"],
    "GBPNZD":  ["GBPNZD", "GBP/NZD"],
    "AUDCAD":  ["AUDCAD", "AUD/CAD"],
    "AUDCHF":  ["AUDCHF", "AUD/CHF"],
    "AUDNZD":  ["AUDNZD", "AUD/NZD"],
    "NZDCAD":  ["NZDCAD", "NZD/CAD"],
    "NZDCHF":  ["NZDCHF", "NZD/CHF"],
    "CADCHF":  ["CADCHF", "CAD/CHF"],

    # Metals
    "XAUUSD":  ["XAUUSD", "GOLD", "XAU/USD", "XAUUSD.", "GOLDm", "XAUUSDC"],
    "XAGUSD":  ["XAGUSD", "SILVER", "XAG/USD", "XAGUSD.", "SILVERm"],
    "XPTUSD":  ["XPTUSD", "PLATINUM", "XPT/USD"],
    "XPDUSD":  ["XPDUSD", "PALLADIUM", "XPD/USD"],

    # Energies
    "XTIUSD":  ["XTIUSD", "USOUSD", "USOIL", "WTI", "CL", "OIL", "CRUDEOIL", "OILUSD", "WTIOIL"],
    "XBRUSD":  ["XBRUSD", "UKOUSD", "UKOIL", "BRENT", "BRENTOIL", "OIL.UK", "BRTUSD"],

    # US Indices
    "US100":   ["US100", "NAS100", "NASDAQ", "USTEC", "NDX", "NDX100", "USTECH",
                "USTECH100", "NAS100m", "US100m", "NASDAQ100", "NQ100", "NASUSD"],
    "US30":    ["US30", "DJ30", "DOW30", "DOWJONES", "DJI", "WALL30", "DJ", "DOW",
                "DJIA", "US30m", "DJ30m"],
    "US500":   ["US500", "SP500", "SPX500", "S&P500", "SPX", "SP500m", "US500m",
                "S&P", "SPXUSD", "SP500USD"],
    "US2000":  ["US2000", "RUSSELL2000", "RUT", "R2000"],

    # European Indices
    "GER40":   ["GER40", "DAX40", "DAX", "DE40", "GER30", "DAX30", "DAXUSD",
                "GER40m", "DAX40m", "GER40.cash", "DAX.cash"],
    "UK100":   ["UK100", "FTSE100", "FTSE", "UK100m", "FTSE100m", "UKX"],
    "FRA40":   ["FRA40", "CAC40", "CAC", "FRA40m", "CAC40m", "CACUSD"],
    "ESP35":   ["ESP35", "IBEX35", "IBEX", "ESP35m"],
    "EU50":    ["EU50", "EUROSTOXX50", "SX5E", "STOXX50", "ESTX50"],
    "ITA40":   ["ITA40", "FTMIB", "IT40", "MIB40"],
    "NED25":   ["NED25", "AEX25", "AEX", "NL25"],
    "SWI20":   ["SWI20", "SMI20", "SMI"],

    # Asian / Pacific Indices
    "JP225":   ["JP225", "JPN225", "NIKKEI", "N225", "NI225", "NIKK", "JP225m"],
    "AUS200":  ["AUS200", "ASX200", "AUS200m", "ASX"],
    "HK50":    ["HK50", "HSI50", "HANGSENG", "HSI", "HK50m"],
    "CHINA50": ["CHINA50", "CN50", "CHINA50m", "FTXIN9"],
    "SGP30":   ["SGP30", "STI30", "STI"],

    # Crypto (24/7 — MT5 CFDs). Included so that when TRADING_MODE=full enables
    # the crypto category, these symbols flow through the discovery pass and get
    # their live broker constraints (contract_size, volume_min, tick value/size)
    # cached — the gap that left crypto sizing/heat on config placeholders.
    "BTCUSD":  ["BTCUSD", "BTC/USD", "BTC_USD", "XBTUSD"],
    "ETHUSD":  ["ETHUSD", "ETH/USD", "ETH_USD"],
    "LTCUSD":  ["LTCUSD", "LTC/USD", "LTC_USD"],
    "XRPUSD":  ["XRPUSD", "XRP/USD", "XRP_USD"],
    "BNBUSD":  ["BNBUSD", "BNB/USD", "BNB_USD"],
    "SOLUSD":  ["SOLUSD", "SOL/USD", "SOL_USD"],
    "ADAUSD":  ["ADAUSD", "ADA/USD", "ADA_USD"],
    "DOTUSD":  ["DOTUSD", "DOT/USD", "DOT_USD"],

    # Deriv synthetics — these stay as-is (handled by deriv.json)
}

# Strip these suffixes when fuzzy-matching broker symbols to our aliases
_STRIP_SUFFIXES = [
    ".cash", ".raw", ".ecn", ".stp", ".pro", "#", "m",
    "_SB", "_i", "!", ".c", ".r", "USD", "cash",
]

# ─────────────────────────────────────────────────────────────────────────────
# Auto-discovery engine
# ─────────────────────────────────────────────────────────────────────────────

class BrokerAutoDiscovery:
    """
    Connects to a live MT5 terminal, pulls the full symbol list,
    matches every symbol to APEX canonical names, and writes
    the broker JSON — completely automatically.

    On startup: runs silently if JSON is already fresh.
    On new broker: detects unknown broker name, runs full scan, writes JSON.
    On broker update: detects stale JSON (symbols changed), re-runs scan.
    """

    def __init__(self, broker_name: str):
        self.broker_name = broker_name.lower()
        self.config_path = _BROKERS_DIR / f"{self.broker_name}.json"

    def run(self, force: bool = False) -> dict:
        """
        Main entry point. Returns the resolved overrides dict.

        Always runs (and overwrites) when:
          - JSON file does not exist
          - JSON has no overrides (empty/corrupt)
          - force=True (manual trigger or --rediscover flag)
          - JSON is older than 7 days (broker may have added/removed symbols)

        Skips only when JSON is fresh, non-empty, and force=False.
        Always overwrites the file when it does run — that is the point.
        """
        if not force and self._is_fresh():
            logger.info(
                "Broker config for '{}' is up to date — skipping auto-discovery",
                self.broker_name,
            )
            return self._load_existing()

        reason = "forced" if force else (
            "file missing" if not self.config_path.exists() else
            "file empty/corrupt" if not self._load_existing() else
            "config stale (7+ days)"
        )
        logger.info(
            "Running broker auto-discovery for '{}' — reason: {} ...",
            self.broker_name, reason,
        )

        # Broker I/O is owned by the Execution Division (platforms/mt5) — pull
        # the full broker symbol list through the gateway helper instead of
        # importing MetaTrader5 here.
        from platforms.mt5.mt5_discovery import (
            list_broker_symbols,
            discover_symbol_constraints,
        )

        broker_symbol_names = list_broker_symbols()
        if not broker_symbol_names:
            logger.warning("No broker symbols discovered — skipping auto-discovery")
            return {}

        logger.info(
            "MT5 broker '{}' has {} total symbols — matching against APEX registry ...",
            self.broker_name, len(broker_symbol_names),
        )

        overrides = self._match_all(broker_symbol_names)

        # ── Discover symbol constraints (stops_level, volume, tick value) ──
        # Resolve over the FULL override universe — freshly matched symbols PLUS
        # any preserved manual/earlier overrides (e.g. crypto that was mapped
        # before its category was auto-matched). Otherwise a symbol present in
        # the overrides map but not re-matched this pass (crypto in the shipped
        # broker JSON) never gets its broker-truth constraints cached, which is
        # exactly what left crypto sizing/heat running on config placeholders.
        resolved = {**self._load_existing(), **overrides}
        constraints = discover_symbol_constraints(list(resolved.values()))

        if constraints:
            logger.info(
                "MT5 symbol constraints discovered — {} symbols",
                len(constraints),
            )

        self._write_json(overrides, constraints)

        matched = len(overrides)
        total = len(APEX_ALIASES)
        logger.info(
            "Auto-discovery complete: {}/{} APEX symbols matched on broker '{}'",
            matched, total, self.broker_name,
        )

        unmatched = [k for k in APEX_ALIASES if k not in overrides]
        if unmatched:
            logger.warning(
                "These APEX symbols were NOT found on broker '{}': {}",
                self.broker_name, ", ".join(unmatched),
            )

        return overrides

    # ── Matching logic ────────────────────────────────────────────────────

    def _match_all(self, broker_symbols: list[str]) -> dict[str, str]:
        """Match every APEX canonical symbol to a broker symbol."""
        overrides: dict[str, str] = {}

        # Build a normalised lookup: stripped_lower → original broker name
        normalised: dict[str, str] = {}
        for sym in broker_symbols:
            norm = self._normalise(sym)
            normalised[norm] = sym  # last one wins if collision

        for apex_name, aliases in APEX_ALIASES.items():
            match = self._find_match(apex_name, aliases, broker_symbols, normalised)
            if match:
                overrides[apex_name] = match

        return overrides

    def _find_match(
        self,
        apex_name: str,
        aliases: list[str],
        broker_symbols: list[str],
        normalised: dict[str, str],
    ) -> Optional[str]:
        """
        Resolution order:
        1. Exact match (case-insensitive) against any alias
        2. Normalised match (strip suffixes, remove separators)
        3. Token match — alias appears in the broker symbol as a whole token,
           bounded by string start/end or a non-alphanumeric separator.

        Greedy substring matching is deliberately avoided: a bare ``in`` test
        let "US30" swallow "US300"/"US3000" and "OIL" swallow "OILGBP", silently
        mapping the wrong instrument. Token matching plus an ambiguity guard
        (multiple distinct broker symbols matching the same APEX name → skip)
        means an uncertain mapping is never written.
        """
        broker_lower = {s.lower(): s for s in broker_symbols}

        # 1. Exact match
        for alias in aliases:
            if alias.lower() in broker_lower:
                return broker_lower[alias.lower()]

        # 2. Normalised match
        for alias in aliases:
            norm = self._normalise(alias)
            if norm in normalised:
                return normalised[norm]

        # 3. Token match — catches "XAUUSD.raw", "GOLD.spot" etc. where the
        #    alias is a whole token, but never a numeric-suffixed neighbour.
        candidates: dict[str, str] = {}  # lower-key → original broker symbol
        for alias in aliases:
            for broker_orig in broker_symbols:
                if self._token_match(alias, broker_orig):
                    candidates[broker_orig.lower()] = broker_orig
        if len(candidates) == 1:
            return next(iter(candidates.values()))
        if len(candidates) > 1:
            logger.warning(
                "[autodiscovery] ambiguous match for '{}' — {} broker symbols "
                "matched ({}); skipping to avoid a wrong mapping",
                apex_name, len(candidates), ", ".join(sorted(candidates.values())),
            )

        return None

    @staticmethod
    def _token_match(alias: str, broker_symbol: str) -> bool:
        """True if *alias* occurs in *broker_symbol* as a whole token.

        The alias must be bounded on both sides by the string start/end or a
        non-alphanumeric character, so "US30" matches "US30.cash" but never
        "US300", and "OIL" matches "OIL_USD" but never "OILGBP". Separator
        characters inside the alias (e.g. "EUR/USD") are matched literally.
        """
        a = alias.upper().strip()
        if not a:
            return False
        pattern = r"(?<![A-Z0-9])" + re.escape(a) + r"(?![A-Z0-9])"
        return re.search(pattern, broker_symbol.upper()) is not None

    def _normalise(self, symbol: str) -> str:
        """Strip prefixes, suffixes, separators, case — down to a bare core."""
        s = symbol.upper()
        # Handle slash-separated pairs: GBP/USD → GBPUSD
        s = s.replace("/", "").replace("_", "").replace("-", "").replace(" ", "")
        # Strip known broker prefixes (e.g. frxEURUSD → EURUSD, frxGBPUSD → GBPUSD)
        # Must be followed by at least 6 chars to avoid stripping too much
        for prefix in ["FRX", "FX"]:
            if s.startswith(prefix) and len(s) >= len(prefix) + 6:
                s = s[len(prefix):]
                break
        # Strip trailing N only if it follows a number (BOOM500N → BOOM500)
        s = re.sub(r"(\d+)N$", r"\1", s)
        # Strip known suffixes longest-first — only if core remains >= 4 chars
        for suffix in sorted(_STRIP_SUFFIXES, key=len, reverse=True):
            su = suffix.upper().replace("/","").replace("_","").replace("-","").replace(" ","")
            if su and s.endswith(su) and len(s) - len(su) >= 4:
                s = s[: -len(su)]
                break
        return s.strip()

    # ── JSON management ───────────────────────────────────────────────────

    def _is_fresh(self) -> bool:
        """
        JSON is fresh only if ALL of these are true:
          1. File exists
          2. File has actual overrides (not empty/corrupt)
          3. File is less than 7 days old
        If any condition fails — return False so discovery runs and overwrites.
        """
        if not self.config_path.exists():
            return False
        import time
        try:
            with open(self.config_path) as f:
                data = json.load(f)
            if not data.get("overrides"):
                return False  # Empty overrides — must re-run
        except Exception as exc:
            logger.warning("[autodiscovery] config file read/parse failed, will re-run discovery: {}", exc)
            return False  # Corrupt file — must re-run
        age_days = (time.time() - self.config_path.stat().st_mtime) / 86400
        return age_days < 7

    def _load_existing(self) -> dict:
        try:
            with open(self.config_path) as f:
                data = json.load(f)
            return data.get("overrides", {})
        except Exception as exc:
            logger.warning("[autodiscovery] failed to load existing overrides: {}", exc)
            return {}

    def _write_json(self, overrides: dict, constraints: dict | None = None) -> None:
        """
        Write the broker JSON — always overwrites when called.
        Preserves manually-added overrides that auto-discovery didn't find,
        but auto-discovered symbols always win (broker is the source of truth).
        Also writes per-symbol constraints (stops_level, volume) discovered live.
        """
        _BROKERS_DIR.mkdir(parents=True, exist_ok=True)

        # Load existing to preserve manual-only entries and rules section
        existing: dict = {}
        if self.config_path.exists():
            try:
                with open(self.config_path) as f:
                    existing = json.load(f)
            except Exception as exc:
                logger.warning("[autodiscovery] failed to read existing config for merge: {}", exc)
                pass

        # Merge: auto-discovered overrides win, but manual-only keys are kept
        merged_overrides = {**existing.get("overrides", {}), **overrides}

        # Merge constraints: newly discovered win over stale cached values
        merged_constraints = {**existing.get("symbol_constraints", {}), **(constraints or {})}

        config = {
            "name": self.broker_name,
            "_auto_discovered": True,
            "_note": "This file is auto-generated on startup. Manual entries are preserved.",
            "rules": existing.get("rules", {
                "forex": {
                    "pattern": "{symbol}",
                    "description": "Passthrough — auto-discovery handles everything"
                }
            }),
            "overrides": merged_overrides,
            "symbol_constraints": merged_constraints,
        }

        with open(self.config_path, "w") as f:
            json.dump(config, f, indent=2)

        logger.info(
            "Broker config written → {} ({} overrides, {} constraints)",
            self.config_path, len(merged_overrides), len(merged_constraints),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Deriv auto-discovery (WebSocket based)
# ─────────────────────────────────────────────────────────────────────────────

class DerivAutoDiscovery:
    """
    Calls the Deriv API to get all active symbols and matches them
    to APEX synthetic names. Updates deriv.json automatically.
    """

    DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"

    # What Deriv might call our canonical synthetics
    DERIV_ALIASES: dict[str, list[str]] = {
        "V10_1S":   ["1HZ10V", "R_10"],
        "V25_1S":   ["1HZ25V", "R_25"],
        "V50_1S":   ["1HZ50V", "R_50"],
        "V75_1S":   ["1HZ75V", "R_75"],
        "V100_1S":  ["1HZ100V", "R_100"],
        "BOOM300":  ["BOOM300", "BOOM300N"],
        "BOOM500":  ["BOOM500", "BOOM500N"],
        "BOOM1000": ["BOOM1000", "BOOM1000N"],
        "CRASH300": ["CRASH300", "CRASH300N"],
        "CRASH500": ["CRASH500", "CRASH500N"],
        "CRASH1000":["CRASH1000", "CRASH1000N"],
        "STPIDX":  ["stpRNG", "STPIDX"],
        "RNGBULL": ["RDBULL", "RNGBULL"],
        "RNGBEAR": ["RDBEAR", "RNGBEAR"],
        "JD10":    ["JD10"],
        "JD25":    ["JD25"],
        "JD50":    ["JD50"],
        "JD75":    ["JD75"],
        "JD100":   ["JD100"],
    }

    def run(self, force: bool = False) -> None:
        config_path = _BROKERS_DIR / "deriv.json"

        if not force:
            import time
            if config_path.exists():
                age = (time.time() - config_path.stat().st_mtime) / 86400
                if age < 7:
                    logger.info("Deriv config is up to date — skipping auto-discovery")
                    return

        logger.info("Running Deriv symbol auto-discovery ...")

        try:
            import asyncio
            asyncio.get_event_loop().run_until_complete(self._run_async(config_path))
        except Exception as exc:
            logger.warning("Deriv auto-discovery failed: {} — using existing config", exc)

    async def _run_async(self, config_path: Path) -> None:
        try:
            import websockets
            import json as _json

            async with websockets.connect(self.DERIV_WS_URL) as ws:
                await ws.send(_json.dumps({"active_symbols": "brief", "product_type": "basic"}))
                resp = _json.loads(await ws.recv())

                if "error" in resp:
                    logger.warning("Deriv symbols API error: {}", resp["error"])
                    return

                active = {s["symbol"] for s in resp.get("active_symbols", [])}
                logger.info("Deriv has {} active symbols", len(active))

                overrides: dict[str, str] = {}

                # Always keep forex prefix rule
                existing_overrides: dict = {}
                if config_path.exists():
                    try:
                        with open(config_path) as f:
                            existing_overrides = _json.load(f).get("overrides", {})
                    except Exception as exc:
                        logger.warning("[autodiscovery] failed to load existing Deriv overrides: {}", exc)
                        pass

                for apex_name, aliases in self.DERIV_ALIASES.items():
                    for alias in aliases:
                        if alias in active:
                            overrides[apex_name] = alias
                            break

                # Keep forex and metal overrides from existing config
                for k, v in existing_overrides.items():
                    if k.startswith("X") or k in ("XAUUSD", "XAGUSD"):
                        overrides[k] = v

                # ── Discover multiplier constraints per symbol ──────────────
                multipliers: dict[str, dict] = {}
                for apex_name, broker_symbol in overrides.items():
                    try:
                        await ws.send(_json.dumps({
                            "contracts_for": broker_symbol,
                            "currency": "USD",
                            "product_type": "basic",
                        }))
                        cfor = _json.loads(await ws.recv())
                        available = cfor.get("contracts_for", {}).get("available", [])
                        for contract in available:
                            if contract.get("contract_type") in ("MULTUP", "MULTDOWN"):
                                mult_list = contract.get("multiplier_range", [])
                                if mult_list:
                                    valid = sorted(int(m) for m in mult_list)
                                    multipliers[broker_symbol] = {
                                        "valid": valid,
                                        "default": valid[0],  # most conservative
                                    }
                                break
                    except Exception as exc:
                        logger.debug("Could not fetch contracts_for {}: {}", broker_symbol, exc)

                if multipliers:
                    multipliers["_default"] = {"valid": [100], "default": 100}
                    logger.info(
                        "Deriv multiplier constraints discovered — {} symbols",
                        len(multipliers) - 1,
                    )

                config = {
                    "name": "deriv",
                    "_auto_discovered": True,
                    "_note": "Auto-generated on startup.",
                    "rules": {
                        "forex": {
                            "pattern": "frx{symbol}",
                            "description": "Deriv forex pairs use frx prefix"
                        }
                    },
                    "overrides": overrides,
                    "multipliers": multipliers,
                }

                _BROKERS_DIR.mkdir(parents=True, exist_ok=True)
                with open(config_path, "w") as f:
                    _json.dump(config, f, indent=2)

                logger.info(
                    "Deriv config updated — {} synthetics mapped", len(overrides)
                )

        except Exception as exc:
            logger.warning("Deriv WebSocket error during discovery: {}", exc)


# ─────────────────────────────────────────────────────────────────────────────
# One-call startup entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_autodiscovery(mt5_broker_name: str = "auto", force: bool = False) -> None:
    """
    Call this once at system startup.
    Discovers MT5 broker name automatically if mt5_broker_name="auto".
    Writes all broker JSONs. Silent if configs are fresh.
    """

    # ── MT5 broker detection ──────────────────────────────────────────────
    # Broker I/O is owned by the Execution Division (platforms/mt5) — detect
    # the broker slug through the gateway helper instead of importing
    # MetaTrader5 here.
    if mt5_broker_name == "auto":
        from platforms.mt5.mt5_discovery import detect_broker_slug

        detected = detect_broker_slug()
        mt5_broker_name = detected or "auto"

    if mt5_broker_name and mt5_broker_name != "auto":
        BrokerAutoDiscovery(mt5_broker_name).run(force=force)

    # ── Deriv discovery ───────────────────────────────────────────────────
    DerivAutoDiscovery().run(force=force)
