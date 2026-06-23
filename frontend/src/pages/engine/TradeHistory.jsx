import { useMemo, useState } from "react";

import { getPerformance, getTradeHistory } from "../../api/engine";
import EquityChart from "../../components/EquityChart";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { badgeClass, dirText } from "../../utils/engineFormat";

const FILTERS = ["ALL", "WIN", "LOSS"];
const PER_PAGE = 50;

function fmtDate(d) {
  if (!d) return "—";
  try {
    const dt = new Date(d);
    return (
      dt.toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
      " " +
      dt.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" })
    );
  } catch {
    return "—";
  }
}

function fmtDur(mins) {
  if (!mins) return "—";
  if (mins < 60) return `${mins.toFixed(0)}m`;
  return `${Math.floor(mins / 60)}h ${Math.floor(mins % 60)}m`;
}

function WinLossSplit({ wins, losses }) {
  const total = wins + losses;
  if (total === 0) {
    return (
      <div className="flex h-72 items-center justify-center text-sm text-gray-500">
        No closed trades yet.
      </div>
    );
  }
  const winPct = (wins / total) * 100;
  return (
    <div className="flex h-72 flex-col items-center justify-center gap-6">
      <div className="text-center">
        <div className="text-4xl font-bold text-emerald-400">
          {winPct.toFixed(1)}%
        </div>
        <div className="mt-1 text-xs uppercase tracking-wider text-gray-500">
          Win Rate
        </div>
      </div>
      <div className="flex h-3 w-full max-w-md overflow-hidden rounded-full bg-gray-700">
        <div className="h-full bg-emerald-500" style={{ width: `${winPct}%` }} />
        <div className="h-full bg-red-500" style={{ width: `${100 - winPct}%` }} />
      </div>
      <div className="flex gap-8 text-sm">
        <span className="text-emerald-400">{wins} wins</span>
        <span className="text-red-400">{losses} losses</span>
      </div>
    </div>
  );
}

export default function TradeHistory() {
  const { data: histData, loading, error, unavailable } = useEnginePoll(
    getTradeHistory,
    10000
  );
  const { data: perfData } = useEnginePoll(getPerformance, 10000);

  const [filter, setFilter] = useState("ALL");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);

  const trades = histData?.trades || [];
  const p = perfData || {};

  const filtered = useMemo(() => {
    let list = trades;
    if (filter === "WIN") list = list.filter((t) => t.outcome === "WIN");
    if (filter === "LOSS") list = list.filter((t) => t.outcome === "LOSS");
    if (search)
      list = list.filter((t) =>
        (t.instrument || "").toLowerCase().includes(search.toLowerCase())
      );
    return list;
  }, [trades, filter, search]);

  const pageCount = Math.ceil(filtered.length / PER_PAGE);
  const paged = filtered.slice(page * PER_PAGE, (page + 1) * PER_PAGE);

  const equityCurve = (p.equity_curve || []).map((pt) => ({
    equity: pt.equity ?? pt.value ?? 0,
    closed_at: pt.closed_at ?? pt.date ?? pt.time,
  }));

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Trade History"
        subtitle="Closed positions and performance breakdown"
      />

      <EngineNotice
        loading={loading}
        unavailable={unavailable}
        error={error}
        hasData={!!histData}
      />

      {histData && (
        <>
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4 lg:grid-cols-7">
            <StatTile label="Total Trades" value={p.total_trades || trades.length} />
            <StatTile
              label="Win Rate"
              value={`${(p.win_rate || 0).toFixed(1)}%`}
              accent="text-emerald-400"
            />
            <StatTile
              label="Profit Factor"
              value={(p.profit_factor || 0).toFixed(2)}
            />
            <StatTile
              label="Avg Win"
              value={`${(p.avg_win_pips || 0).toFixed(1)} pips`}
              accent="text-emerald-400"
            />
            <StatTile
              label="Avg Loss"
              value={`${(p.avg_loss_pips || 0).toFixed(1)} pips`}
              accent="text-red-400"
            />
            <StatTile
              label="Best Trade"
              value={`${(p.best_trade_pips || 0).toFixed(1)} pips`}
              accent="text-emerald-400"
            />
            <StatTile
              label="Worst Trade"
              value={`${(p.worst_trade_pips || 0).toFixed(1)} pips`}
              accent="text-red-400"
            />
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {FILTERS.map((f) => (
              <button
                key={f}
                type="button"
                onClick={() => {
                  setFilter(f);
                  setPage(0);
                }}
                className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                  filter === f
                    ? "bg-emerald-600/20 text-emerald-400"
                    : "bg-gray-800 text-gray-400 hover:bg-gray-700/60 hover:text-gray-100"
                }`}
              >
                {f}
              </button>
            ))}
            <input
              type="text"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setPage(0);
              }}
              placeholder="Search instrument…"
              className="ml-auto w-48 rounded-md border border-gray-700 bg-gray-800 px-3 py-1.5 text-sm text-gray-100 outline-none placeholder:text-gray-500 focus:border-emerald-500"
            />
          </div>

          <Panel className="overflow-x-auto p-0">
            <table className="w-full min-w-[900px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="px-4 py-3">Date/Time</th>
                  <th className="px-2 py-3">Instrument</th>
                  <th className="px-2 py-3">Direction</th>
                  <th className="px-2 py-3 text-right">Entry</th>
                  <th className="px-2 py-3 text-right">Exit</th>
                  <th className="px-2 py-3 text-right">Pips</th>
                  <th className="px-2 py-3 text-right">P&L ($)</th>
                  <th className="px-2 py-3 text-right">Duration</th>
                  <th className="px-2 py-3 text-right">Score</th>
                  <th className="px-2 py-3">Exit Reason</th>
                  <th className="px-2 py-3">Outcome</th>
                </tr>
              </thead>
              <tbody>
                {paged.length === 0 && (
                  <tr>
                    <td colSpan={11} className="px-4 py-10 text-center text-gray-500">
                      No trades match filter.
                    </td>
                  </tr>
                )}
                {paged.map((t) => {
                  const pnl = t.pnl_dollars || 0;
                  const dir = (t.direction || "").toUpperCase();
                  const exitReason = t.exit_reason || "—";
                  const rawBroker = t.raw_broker_reason || "";
                  const mismatch =
                    rawBroker && exitReason !== rawBroker && exitReason !== "—";
                  return (
                    <tr key={t.id} className="border-b border-gray-800">
                      <td className="px-4 py-2 text-xs text-gray-400">
                        {fmtDate(t.opened_at)}
                      </td>
                      <td className="px-2 py-2 font-semibold text-gray-100">
                        {t.instrument}
                      </td>
                      <td className={`px-2 py-2 font-medium ${dirText(dir)}`}>
                        {dir}
                      </td>
                      <td className="px-2 py-2 text-right font-mono text-xs">
                        {(t.entry_price || 0).toFixed(5)}
                      </td>
                      <td className="px-2 py-2 text-right font-mono text-xs">
                        {(t.exit_price || 0).toFixed(5)}
                      </td>
                      <td
                        className={`px-2 py-2 text-right font-mono text-xs ${
                          (t.pnl_pips || 0) >= 0 ? "text-emerald-400" : "text-red-400"
                        }`}
                      >
                        {(t.pnl_pips || 0).toFixed(1)}
                      </td>
                      <td
                        className={`px-2 py-2 text-right font-mono text-xs ${
                          pnl >= 0 ? "text-emerald-400" : "text-red-400"
                        }`}
                      >
                        {pnl >= 0 ? "+" : "-"}${Math.abs(pnl).toFixed(2)}
                      </td>
                      <td className="px-2 py-2 text-right font-mono text-xs">
                        {fmtDur(t.duration_minutes)}
                      </td>
                      <td className="px-2 py-2 text-right font-mono text-xs">
                        {t.score || 0}
                      </td>
                      <td
                        className="px-2 py-2 font-mono text-xs"
                        title={rawBroker ? `Broker: ${rawBroker}` : undefined}
                      >
                        <span
                          className={mismatch ? "text-yellow-400" : "text-gray-400"}
                        >
                          {mismatch ? "⚠ " : ""}
                          {exitReason}
                        </span>
                      </td>
                      <td className="px-2 py-2">
                        <span
                          className={badgeClass(t.outcome === "WIN" ? "green" : "red")}
                        >
                          {t.outcome}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Panel>

          {pageCount > 1 && (
            <div className="flex items-center justify-center gap-4 text-sm text-gray-400">
              <button
                type="button"
                disabled={page === 0}
                onClick={() => setPage(page - 1)}
                className="rounded-md bg-gray-800 px-3 py-1.5 text-xs font-medium text-gray-300 disabled:opacity-40 enabled:hover:bg-gray-700/60"
              >
                ← Prev
              </button>
              <span>
                Page {page + 1} of {pageCount}
              </span>
              <button
                type="button"
                disabled={page >= pageCount - 1}
                onClick={() => setPage(page + 1)}
                className="rounded-md bg-gray-800 px-3 py-1.5 text-xs font-medium text-gray-300 disabled:opacity-40 enabled:hover:bg-gray-700/60"
              >
                Next →
              </button>
            </div>
          )}

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Panel title="Cumulative P&L">
              <EquityChart data={equityCurve} />
            </Panel>
            <Panel title="Win / Loss Distribution">
              <WinLossSplit
                wins={p.win_count || p.wins || 0}
                losses={p.loss_count || p.losses || 0}
              />
            </Panel>
          </div>
        </>
      )}
    </div>
  );
}
