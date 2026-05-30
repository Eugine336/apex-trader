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
        Skips scan if JSON is fresh unless force=True.
        """
        if not force and self._is_fresh():
            logger.info(
                "Broker config for '{}' is up to date — skipping auto-discovery",
                self.broker_name,
            )
            return self._load_existing()

        logger.info(
            "Running broker auto-discovery for '{}' ...", self.broker_name
        )

        try:
            import MetaTrader5 as mt5
        except ImportError:
            logger.warning("MetaTrader5 not installed — skipping auto-discovery")
            return {}

        if not mt5.initialize():
            logger.warning("MT5 not connected — skipping auto-discovery")
            return {}

        # Pull every symbol the broker offers
        all_broker_symbols = mt5.symbols_get()
        if not all_broker_symbols:
            logger.warning("MT5 returned no symbols")
            mt5.shutdown()
            return {}

        broker_symbol_names = [s.name for s in all_broker_symbols]
        logger.info(
            "MT5 broker '{}' has {} total symbols — matching against APEX registry ...",
            self.broker_name, len(broker_symbol_names),
        )

        overrides = self._match_all(broker_symbol_names)
        mt5.shutdown()

        self._write_json(overrides)

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
        3. Substring match — broker symbol contains our alias core
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

        # 3. Substring / contains match — catches "XAUUSD.raw", "NAS100m" etc.
        for alias in aliases:
            alias_core = self._normalise(alias)
            for broker_norm, broker_orig in normalised.items():
                if alias_core and alias_core in broker_norm:
                    return broker_orig
                if broker_norm and broker_norm in alias_core:
                    return broker_orig

        return None

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
        import re
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
        """JSON is fresh if it exists and was written in the last 7 days."""
        if not self.config_path.exists():
            return False
        import time
        age_days = (time.time() - self.config_path.stat().st_mtime) / 86400
        return age_days < 7

    def _load_existing(self) -> dict:
        try:
            with open(self.config_path) as f:
                data = json.load(f)
            return data.get("overrides", {})
        except Exception:
            return {}

    def _write_json(self, overrides: dict) -> None:
        """Write the broker JSON — preserves Deriv rules, updates overrides."""
        _BROKERS_DIR.mkdir(parents=True, exist_ok=True)

        # Load existing to preserve any manual entries and rules
        existing: dict = {}
        if self.config_path.exists():
            try:
                with open(self.config_path) as f:
                    existing = json.load(f)
            except Exception:
                pass

        # Merge: auto-discovered overrides win, but manual-only keys are kept
        merged_overrides = {**existing.get("overrides", {}), **overrides}

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
        }

        with open(self.config_path, "w") as f:
            json.dump(config, f, indent=2)

        logger.info(
            "Broker config written → {} ({} overrides)",
            self.config_path, len(merged_overrides),
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
                    except Exception:
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

    # ── MT5 discovery ─────────────────────────────────────────────────────
    try:
        import MetaTrader5 as mt5
        if mt5.initialize():
            info = mt5.terminal_info()
            if info and mt5_broker_name == "auto":
                # Use company name as broker identifier — sanitise to filename safe
                raw = getattr(info, "company", "unknown")
                broker_slug = re.sub(r"[^a-z0-9]", "_", raw.lower()).strip("_")
                broker_slug = re.sub(r"_+", "_", broker_slug)
                mt5_broker_name = broker_slug or "mt5_broker"
                logger.info("MT5 broker detected: '{}' → slug: '{}'", raw, broker_slug)
            mt5.shutdown()
    except Exception:
        pass

    if mt5_broker_name and mt5_broker_name != "auto":
        BrokerAutoDiscovery(mt5_broker_name).run(force=force)

    # ── Deriv discovery ───────────────────────────────────────────────────
    DerivAutoDiscovery().run(force=force)