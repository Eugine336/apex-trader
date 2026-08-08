import { useCallback, useEffect, useMemo, useState } from "react";

import { extractError } from "../api/client";
import { getHistory } from "../api/dashboard";
import TradesTable from "../components/TradesTable";

const PAGE_SIZE = 50;

export default function Trades() {
  const [trades, setTrades] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [page, setPage] = useState(0);

  // Filters
  const [symbol, setSymbol] = useState("");
  const [direction, setDirection] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const rows = await getHistory({
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        symbol: symbol || undefined,
        direction: direction || undefined,
        since: since ? new Date(since).toISOString() : undefined,
        until: until ? new Date(until).toISOString() : undefined,
      });
      setTrades(rows);
      setError("");
    } catch (err) {
      setError(extractError(err, "Failed to load trades"));
    } finally {
      setLoading(false);
    }
  }, [page, symbol, direction, since, until]);

  useEffect(() => {
    load();
  }, [load]);

  // Symbol options derived from the current page (best-effort convenience).
  const symbolOptions = useMemo(() => {
    const set = new Set(trades.map((t) => t.symbol).filter(Boolean));
    return Array.from(set).sort();
  }, [trades]);

  const resetFilters = () => {
    setSymbol("");
    setDirection("");
    setSince("");
    setUntil("");
    setPage(0);
  };

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-gray-100">Trade History</h1>

      {error && (
        <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 rounded-lg border border-gray-700 bg-gray-800 p-4 sm:grid-cols-2 lg:grid-cols-5">
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-400">Symbol</label>
          <input
            type="text"
            list="symbol-options"
            value={symbol}
            onChange={(e) => {
              setPage(0);
              setSymbol(e.target.value.toUpperCase());
            }}
            placeholder="e.g. EURUSD"
            className="w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-sm text-white placeholder-gray-500 focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500"
          />
          <datalist id="symbol-options">
            {symbolOptions.map((s) => (
              <option key={s} value={s} />
            ))}
          </datalist>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-400">Direction</label>
          <select
            value={direction}
            onChange={(e) => {
              setPage(0);
              setDirection(e.target.value);
            }}
            className="w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-sm text-white focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500"
          >
            <option value="">All</option>
            <option value="LONG">LONG</option>
            <option value="SHORT">SHORT</option>
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-400">From</label>
          <input
            type="date"
            value={since}
            onChange={(e) => {
              setPage(0);
              setSince(e.target.value);
            }}
            className="w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-sm text-white focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-400">To</label>
          <input
            type="date"
            value={until}
            onChange={(e) => {
              setPage(0);
              setUntil(e.target.value);
            }}
            className="w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-sm text-white focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500"
          />
        </div>
        <div className="flex items-end">
          <button
            type="button"
            onClick={resetFilters}
            className="w-full rounded-md border border-gray-600 px-3 py-2 text-sm font-medium text-gray-300 transition-colors duration-200 hover:bg-gray-700"
          >
            Reset
          </button>
        </div>
      </div>

      {loading ? (
        <div className="animate-pulse text-sm text-gray-400">Loading trades…</div>
      ) : (
        <TradesTable trades={trades} emptyMessage="No trades match these filters." />
      )}

      <div className="flex items-center justify-between">
        <p className="text-sm text-gray-500">
          Page {page + 1}
          {trades.length > 0 ? ` — ${trades.length} shown` : ""}
        </p>
        <div className="flex gap-2">
          <button
            type="button"
            disabled={page === 0}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            className="rounded-md border border-gray-600 px-3 py-2 text-sm font-medium text-gray-300 transition-colors duration-200 hover:bg-gray-700 disabled:opacity-40"
          >
            Previous
          </button>
          <button
            type="button"
            disabled={trades.length < PAGE_SIZE}
            onClick={() => setPage((p) => p + 1)}
            className="rounded-md border border-gray-600 px-3 py-2 text-sm font-medium text-gray-300 transition-colors duration-200 hover:bg-gray-700 disabled:opacity-40"
          >
            Next
          </button>
        </div>
      </div>
    </div>
  );
}
