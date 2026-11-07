import { useCallback, useEffect, useState } from "react";

import { extractError } from "../api/client";
import { getBrokerInstruments, getConfig, updateConfig } from "../api/config";

const CATEGORIES = ["forex", "commodity", "index", "synthetic", "crypto"];
const LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"];

export default function TradingConfig() {
  const [form, setForm] = useState({
    universe_mode: "all",
    enabled_categories: [],
    enabled_symbols_override: "",
    risk_per_trade_pct: "",
    max_daily_drawdown_pct: "",
    max_open_trades: "",
    owner_limits_json: "{}",
    data_path_fixes_enabled: false,
    log_level: "INFO",
  });
  const [brokerInstruments, setBrokerInstruments] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [restartRequired, setRestartRequired] = useState(false);

  const load = useCallback(async () => {
    try {
      const [cfg, catalog] = await Promise.all([
        getConfig(),
        getBrokerInstruments().catch(() => ({ symbols: [] })),
      ]);
      setBrokerInstruments(Array.isArray(catalog?.symbols) ? catalog.symbols : []);
      setForm({
        universe_mode: cfg.universe_mode || (cfg.enabled_symbols_override?.length ? "selected" : "all"),
        enabled_categories: cfg.enabled_categories || [],
        enabled_symbols_override: (cfg.enabled_symbols_override || []).join(", "),
        risk_per_trade_pct: cfg.risk_per_trade_pct ?? "",
        max_daily_drawdown_pct: cfg.max_daily_drawdown_pct ?? "",
        max_open_trades: cfg.max_open_trades ?? "",
        owner_limits_json: JSON.stringify(cfg.owner_limits || {}, null, 2),
        data_path_fixes_enabled: !!cfg.data_path_fixes_enabled,
        log_level: cfg.log_level || "INFO",
      });
      setError("");
    } catch (err) {
      setError(extractError(err, "Failed to load config"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const toggleCategory = (cat) => {
    setForm((s) => ({
      ...s,
      enabled_categories: s.enabled_categories.includes(cat)
        ? s.enabled_categories.filter((c) => c !== cat)
        : [...s.enabled_categories, cat],
    }));
  };

  const toggleSymbol = (symbol, checked) => {
    setForm((current) => {
      const selected = current.enabled_symbols_override
        .split(",")
        .map((value) => value.trim().toUpperCase())
        .filter(Boolean);
      let next;
      if (current.universe_mode === "single") {
        next = checked ? [symbol.toUpperCase()] : [];
      } else {
        next = checked
          ? [...new Set([...selected, symbol.toUpperCase()])]
          : selected.filter((value) => value !== symbol.toUpperCase());
      }
      return { ...current, enabled_symbols_override: next.join(", ") };
    });
  };

  const handleSave = async (e) => {
    e.preventDefault();
    setError("");
    setNotice("");

    let ownerLimits = {};
    try {
      ownerLimits = JSON.parse(form.owner_limits_json || "{}");
      if (!ownerLimits || typeof ownerLimits !== "object" || Array.isArray(ownerLimits)) {
        throw new Error("Owner limits must be a JSON object.");
      }
    } catch (err) {
      setError(`Invalid owner-limits JSON: ${err.message}`);
      return;
    }

    setSaving(true);

    // Build a payload of only the fields the API accepts. Empty numeric
    // strings become null (omitted) so we don't send invalid values.
    const symbols = form.enabled_symbols_override
      .split(",")
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean);

    const payload = {
      universe_mode: form.universe_mode,
      enabled_categories: form.enabled_categories.length
        ? form.enabled_categories
        : null,
      enabled_symbols_override:
        form.universe_mode === "all" ? null : symbols.length ? symbols : null,
      risk_per_trade_pct:
        form.risk_per_trade_pct === "" ? null : Number(form.risk_per_trade_pct),
      max_daily_drawdown_pct:
        form.max_daily_drawdown_pct === ""
          ? null
          : Number(form.max_daily_drawdown_pct),
      max_open_trades:
        form.max_open_trades === "" ? null : Number(form.max_open_trades),
      owner_limits: ownerLimits,
      data_path_fixes_enabled: form.data_path_fixes_enabled,
      log_level: form.log_level,
    };

    try {
      const res = await updateConfig(payload);
      setRestartRequired(!!res.restart_required);
      setNotice(res.message || "Config saved");
    } catch (err) {
      setError(extractError(err, "Failed to save config"));
    } finally {
      setSaving(false);
    }
  };

  const inputClass =
    "w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-sm text-white placeholder-gray-500 focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500";

  if (loading) {
    return <div className="animate-pulse text-sm text-gray-400">Loading…</div>;
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-gray-100">Trading Config</h1>

      {error && (
        <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </div>
      )}
      {notice && (
        <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-400">
          {notice}
        </div>
      )}
      {restartRequired && (
        <div className="rounded-md border border-yellow-500/40 bg-yellow-500/10 px-3 py-2 text-sm text-yellow-400">
          Your instance is running — restart it from the Instance page to apply
          these changes.
        </div>
      )}

      <form
        onSubmit={handleSave}
        className="space-y-6 rounded-lg border border-gray-700 bg-gray-800 p-6 shadow-lg"
      >
        <div>
          <label className="mb-1 block text-sm font-medium text-gray-300">
            Trading Universe
          </label>
          <select
            value={form.universe_mode}
            onChange={(e) =>
              setForm((s) => ({
                ...s,
                universe_mode: e.target.value,
                enabled_symbols_override: e.target.value === "all" ? "" : s.enabled_symbols_override,
              }))
            }
            className={inputClass}
          >
            <option value="all">All eligible instruments</option>
            <option value="selected">Selected instruments (any number)</option>
            <option value="single">One instrument only</option>
          </select>
          <p className="mt-1 text-xs text-gray-500">
            Owner selection defines the trading scope. Broker availability and risk checks still apply.
          </p>
        </div>

        <div>
          <label className="mb-2 block text-sm font-medium text-gray-300">
            Enabled Categories
          </label>
          <div className="flex flex-wrap gap-2">
            {CATEGORIES.map((cat) => {
              const active = form.enabled_categories.includes(cat);
              return (
                <button
                  type="button"
                  key={cat}
                  onClick={() => toggleCategory(cat)}
                  className={`rounded-md border px-3 py-1.5 text-sm font-medium capitalize transition-colors duration-200 ${
                    active
                      ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-400"
                      : "border-gray-600 text-gray-400 hover:bg-gray-700"
                  }`}
                >
                  {cat}
                </button>
              );
            })}
          </div>
          <p className="mt-1 text-xs text-gray-500">
            Leave all unselected to use the instance defaults.
          </p>
        </div>

        <div>
          <label className="mb-2 block text-sm font-medium text-gray-300">
            Broker-Discovered Instruments
          </label>
          {brokerInstruments.filter((item) => item.new_position_eligible).length > 0 ? (
            <div className="grid max-h-64 grid-cols-1 gap-2 overflow-y-auto rounded-md border border-gray-700 p-3 sm:grid-cols-2 lg:grid-cols-3">
              {brokerInstruments
                .filter((item) => item.new_position_eligible)
                .map((item) => {
                  const ownerSymbol = String(item.apex_symbol || item.symbol).toUpperCase();
                  const selected = form.enabled_symbols_override
                    .split(",")
                    .map((value) => value.trim().toUpperCase())
                    .includes(ownerSymbol);
                  return (
                    <label key={item.symbol} className="flex items-start gap-2 text-sm text-gray-300">
                      <input
                        type="checkbox"
                        checked={selected}
                        disabled={form.universe_mode === "all"}
                        onChange={(event) => toggleSymbol(ownerSymbol, event.target.checked)}
                        className="mt-0.5"
                      />
                      <span>
                        <span className="font-medium">{ownerSymbol}</span>
                        {item.symbol !== ownerSymbol && (
                          <span className="ml-1 text-xs text-gray-500">broker: {item.symbol}</span>
                        )}
                        <span className="block text-xs text-gray-500">
                          {item.description || item.path || "MT5 instrument"}
                        </span>
                      </span>
                    </label>
                  );
                })}
            </div>
          ) : (
            <p className="rounded-md border border-gray-700 p-3 text-xs text-gray-500">
              No live MT5 catalogue is available. Start the instance and connect the MT5 terminal to load broker symbols; manual symbol entry remains available below.
            </p>
          )}
          <p className="mt-1 text-xs text-gray-500">
            Only symbols with broker permission for new positions and complete basic contract specifications are listed. A later instrument-capability check still decides whether Apex can trade them safely.
          </p>
        </div>

        <div>
          <label className="mb-1 block text-sm font-medium text-gray-300">
            Symbols Override
          </label>
          <input
            type="text"
            value={form.enabled_symbols_override}
            onChange={(e) =>
              setForm((s) => ({ ...s, enabled_symbols_override: e.target.value }))
            }
            disabled={form.universe_mode === "all"}
            placeholder={form.universe_mode === "single" ? "XAUUSD" : "EURUSD, GBPUSD, XAUUSD"}
            className={inputClass}
          />
          <p className="mt-1 text-xs text-gray-500">
            {form.universe_mode === "single"
              ? "Enter exactly one broker symbol."
              : form.universe_mode === "selected"
                ? "Comma-separated broker symbols. Every requested symbol must be available."
                : "Disabled in all-instruments mode."}
          </p>
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">
              Risk per Trade (%)
            </label>
            <input
              type="number"
              step="0.1"
              min="0"
              max="10"
              value={form.risk_per_trade_pct}
              onChange={(e) =>
                setForm((s) => ({ ...s, risk_per_trade_pct: e.target.value }))
              }
              placeholder="0.75"
              className={inputClass}
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">
              Max Daily Drawdown (%)
            </label>
            <input
              type="number"
              step="0.5"
              min="0"
              max="100"
              value={form.max_daily_drawdown_pct}
              onChange={(e) =>
                setForm((s) => ({ ...s, max_daily_drawdown_pct: e.target.value }))
              }
              placeholder="3"
              className={inputClass}
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">
              Max Open Trades
            </label>
            <input
              type="number"
              step="1"
              min="1"
              max="100"
              value={form.max_open_trades}
              onChange={(e) =>
                setForm((s) => ({ ...s, max_open_trades: e.target.value }))
              }
              placeholder="5"
              className={inputClass}
            />
          </div>
        </div>

        <div>
          <label className="mb-1 block text-sm font-medium text-gray-300">
            Advanced Owner Risk Limits (JSON)
          </label>
          <textarea
            rows={12}
            spellCheck={false}
            value={form.owner_limits_json}
            onChange={(e) =>
              setForm((s) => ({ ...s, owner_limits_json: e.target.value }))
            }
            placeholder={'{\n  "risk_per_trade_pct": 0.5,\n  "max_risk_pct_per_trade": 1.5,\n  "max_total_risk_pct": 4.0,\n  "max_open_positions": 4\n}'}
            className={inputClass + " font-mono"}
          />
          <p className="mt-1 text-xs text-gray-500">
            Optional partial update. Supported keys include per-trade and portfolio risk,
            daily/weekly drawdown, position and correlation caps, campaign exposure,
            hedge limits, margin guards, and cooldown settings. Unknown keys, invalid
            combinations, and attempts to weaken hard safety guards are rejected.
            Changes to a running instance require a restart.
          </p>
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <label className="mb-1 block text-sm font-medium text-gray-300">
              Log Level
            </label>
            <select
              value={form.log_level}
              onChange={(e) => setForm((s) => ({ ...s, log_level: e.target.value }))}
              className={inputClass}
            >
              {LOG_LEVELS.map((lvl) => (
                <option key={lvl} value={lvl}>
                  {lvl}
                </option>
              ))}
            </select>
          </div>
          <div className="flex items-end">
            <label className="flex cursor-pointer items-center gap-3">
              <button
                type="button"
                role="switch"
                aria-checked={form.data_path_fixes_enabled}
                onClick={() =>
                  setForm((s) => ({
                    ...s,
                    data_path_fixes_enabled: !s.data_path_fixes_enabled,
                  }))
                }
                className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors duration-200 ${
                  form.data_path_fixes_enabled ? "bg-emerald-600" : "bg-gray-600"
                }`}
              >
                <span
                  className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform duration-200 ${
                    form.data_path_fixes_enabled ? "translate-x-6" : "translate-x-1"
                  }`}
                />
              </button>
              <span className="text-sm font-medium text-gray-300">
                Data Path Fixes Enabled
              </span>
            </label>
          </div>
        </div>

        <div>
          <button
            type="submit"
            disabled={saving}
            className="rounded-md bg-emerald-600 px-5 py-2 text-sm font-medium text-white transition-colors duration-200 hover:bg-emerald-700 disabled:opacity-50"
          >
            {saving ? "Saving…" : "Save Config"}
          </button>
        </div>
      </form>
    </div>
  );
}
