import { useCallback, useEffect, useMemo, useState } from "react";

import { getTrades, getUsers } from "../api/admin";
import { extractError } from "../api/client";
import {
  directionBadgeClass,
  formatDateTime,
  formatMoney,
  pnlColor,
} from "../utils/format";

const PAGE_SIZE = 50;

export default function AdminTrades() {
  const [trades, setTrades] = useState([]);
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [page, setPage] = useState(0);
  const [userId, setUserId] = useState("");

  useEffect(() => {
    getUsers()
      .then(setUsers)
      .catch(() => {
        // Non-fatal — the dropdown just falls back to raw user IDs.
      });
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const rows = await getTrades({
        userId: userId || undefined,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      });
      setTrades(rows);
      setError("");
    } catch (err) {
      setError(extractError(err, "Failed to load trades"));
    } finally {
      setLoading(false);
    }
  }, [page, userId]);

  useEffect(() => {
    load();
  }, [load]);

  const emailById = useMemo(() => {
    const map = {};
    users.forEach((u) => {
      map[u.id] = u.email;
    });
    return map;
  }, [users]);

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-gray-100">All Trades</h1>

      {error && (
        <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </div>
      )}

      <div className="rounded-lg border border-gray-700 bg-gray-800 p-4">
        <label className="mb-1 block text-xs font-medium text-gray-400">User</label>
        <select
          value={userId}
          onChange={(e) => {
            setPage(0);
            setUserId(e.target.value);
          }}
          className="w-full max-w-xs rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-sm text-white focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500"
        >
          <option value="">All users</option>
          {users.map((u) => (
            <option key={u.id} value={u.id}>
              {u.email}
            </option>
          ))}
        </select>
      </div>

      {loading ? (
        <div className="animate-pulse text-sm text-gray-400">Loading trades…</div>
      ) : trades.length === 0 ? (
        <div className="rounded-lg border border-gray-700 bg-gray-800 p-8 text-center text-sm text-gray-500">
          No trades match this filter.
        </div>
      ) : (
        <div className="apex-scroll overflow-x-auto rounded-lg border border-gray-700">
          <table className="min-w-full divide-y divide-gray-700 text-sm">
            <thead className="bg-gray-800">
              <tr className="text-left text-xs uppercase tracking-wider text-gray-400">
                <th className="px-4 py-3">User</th>
                <th className="px-4 py-3">Symbol</th>
                <th className="px-4 py-3">Direction</th>
                <th className="px-4 py-3 text-right">Entry</th>
                <th className="px-4 py-3 text-right">Exit</th>
                <th className="px-4 py-3 text-right">P&amp;L</th>
                <th className="px-4 py-3 text-right">Pips</th>
                <th className="px-4 py-3">Exit Reason</th>
                <th className="px-4 py-3">Closed</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-700">
              {trades.map((t, i) => (
                <tr
                  key={t.id ?? `${t.ticket}-${i}`}
                  className={i % 2 === 0 ? "bg-gray-800" : "bg-gray-750"}
                >
                  <td className="px-4 py-3 text-gray-300">
                    {emailById[t.user_id] || `#${t.user_id}`}
                  </td>
                  <td className="px-4 py-3 font-medium text-gray-100">{t.symbol}</td>
                  <td className="px-4 py-3">
                    <span
                      className={`inline-block rounded px-2 py-0.5 text-xs font-semibold ${directionBadgeClass(
                        t.direction
                      )}`}
                    >
                      {String(t.direction || "").toUpperCase()}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums text-gray-300">
                    {Number(t.entry_price || 0)}
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums text-gray-300">
                    {Number(t.exit_price || 0)}
                  </td>
                  <td
                    className={`px-4 py-3 text-right tabular-nums font-semibold ${pnlColor(
                      t.pnl
                    )}`}
                  >
                    {formatMoney(t.pnl)}
                  </td>
                  <td
                    className={`px-4 py-3 text-right tabular-nums ${pnlColor(t.pnl_pips)}`}
                  >
                    {Number(t.pnl_pips || 0).toFixed(1)}
                  </td>
                  <td className="px-4 py-3 text-gray-400">{t.exit_reason || "—"}</td>
                  <td className="px-4 py-3 text-gray-400">
                    {formatDateTime(t.closed_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
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
