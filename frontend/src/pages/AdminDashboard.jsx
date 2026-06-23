import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  getAggregate,
  getInstances,
  getStats,
  getUsersPerformance,
} from "../api/admin";
import { extractError } from "../api/client";
import DailyPnLChart from "../components/DailyPnLChart";
import EquityChart from "../components/EquityChart";
import StatusBadge from "../components/StatusBadge";
import SummaryCard from "../components/SummaryCard";
import TradeVolumeChart from "../components/TradeVolumeChart";
import {
  formatDateTime,
  formatDuration,
  formatMoney,
  formatPercent,
  pnlColor,
} from "../utils/format";

export default function AdminDashboard() {
  const [stats, setStats] = useState(null);
  const [instances, setInstances] = useState([]);
  const [aggregate, setAggregate] = useState({ equity_curve: [], daily_pnl: [] });
  const [performance, setPerformance] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const [s, inst, agg, perf] = await Promise.all([
        getStats(),
        getInstances(),
        getAggregate({ days: 30 }),
        getUsersPerformance(),
      ]);
      setStats(s);
      setInstances(inst);
      setAggregate(agg);
      setPerformance(perf);
      setError("");
    } catch (err) {
      setError(extractError(err, "Failed to load admin overview"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, [load]);

  if (loading) {
    return <div className="animate-pulse text-sm text-gray-400">Loading admin overview…</div>;
  }

  const running = instances.filter((i) => i.alive);
  const topPerformers = performance.filter((p) => p.total_trades > 0).slice(0, 5);

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-gray-100">Admin Overview</h1>

      {error && (
        <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <SummaryCard
          label="Total Users"
          value={stats?.total_users ?? 0}
          sub={`${stats?.active_users ?? 0} active / ${stats?.admin_users ?? 0} admin`}
        />
        <SummaryCard
          label="Active Instances"
          value={stats?.active_instances ?? 0}
          sub={`${stats?.total_instances ?? 0} total`}
        />
        <SummaryCard
          label="System Uptime"
          value={formatDuration(stats?.system_uptime_seconds)}
        />
        <SummaryCard
          label="Total Trades"
          value={stats?.total_trades ?? 0}
          sub={`${stats?.trades_today ?? 0} today · P&L ${formatMoney(stats?.total_pnl)}`}
          accent={pnlColor(stats?.total_pnl)}
        />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="rounded-lg border border-gray-700 bg-gray-800 p-5 shadow-lg">
          <h2 className="mb-4 text-lg font-semibold text-gray-100">
            Combined Equity Curve
          </h2>
          <EquityChart data={aggregate.equity_curve} />
        </div>
        <div className="rounded-lg border border-gray-700 bg-gray-800 p-5 shadow-lg">
          <h2 className="mb-4 text-lg font-semibold text-gray-100">
            Trade Volume (30d)
          </h2>
          <TradeVolumeChart data={aggregate.daily_pnl} />
        </div>
      </div>

      <div className="rounded-lg border border-gray-700 bg-gray-800 p-5 shadow-lg">
        <h2 className="mb-4 text-lg font-semibold text-gray-100">
          Aggregate Daily P&amp;L (30d)
        </h2>
        <DailyPnLChart data={aggregate.daily_pnl} />
      </div>

      <div>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-100">User Performance</h2>
          <Link
            to="/admin/users"
            className="text-sm font-medium text-emerald-400 hover:text-emerald-300"
          >
            Manage users →
          </Link>
        </div>
        {performance.length === 0 ? (
          <div className="rounded-lg border border-gray-700 bg-gray-800 p-8 text-center text-sm text-gray-500">
            No users yet.
          </div>
        ) : (
          <div className="apex-scroll overflow-x-auto rounded-lg border border-gray-700">
            <table className="min-w-full divide-y divide-gray-700 text-sm">
              <thead className="bg-gray-800">
                <tr className="text-left text-xs uppercase tracking-wider text-gray-400">
                  <th className="px-4 py-3">User</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3 text-right">Trades</th>
                  <th className="px-4 py-3 text-right">Win Rate</th>
                  <th className="px-4 py-3 text-right">P&amp;L</th>
                  <th className="px-4 py-3">Last Active</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-700">
                {performance.map((p, i) => (
                  <tr
                    key={p.user_id}
                    className={i % 2 === 0 ? "bg-gray-800" : "bg-gray-750"}
                  >
                    <td className="px-4 py-3 text-gray-100">
                      {p.email || `user #${p.user_id}`}
                    </td>
                    <td className="px-4 py-3">
                      <StatusBadge status={p.status} />
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums text-gray-300">
                      {p.total_trades}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums text-gray-300">
                      {formatPercent(p.win_rate)}
                    </td>
                    <td
                      className={`px-4 py-3 text-right tabular-nums font-semibold ${pnlColor(
                        p.total_pnl
                      )}`}
                    >
                      {formatMoney(p.total_pnl)}
                    </td>
                    <td className="px-4 py-3 text-gray-400">
                      {p.last_trade_at ? formatDateTime(p.last_trade_at) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {topPerformers.length > 0 && (
        <div>
          <h2 className="mb-3 text-lg font-semibold text-gray-100">Top Performers</h2>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {topPerformers.map((p, i) => (
              <div
                key={p.user_id}
                className="rounded-lg border border-gray-700 bg-gray-800 p-4 shadow-lg"
              >
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium text-gray-300">
                    #{i + 1} · {p.email || `user #${p.user_id}`}
                  </span>
                  <span className={`text-sm font-semibold ${pnlColor(p.total_pnl)}`}>
                    {formatMoney(p.total_pnl)}
                  </span>
                </div>
                <p className="mt-1 text-xs text-gray-500">
                  {p.total_trades} trades · {formatPercent(p.win_rate)} win rate
                </p>
              </div>
            ))}
          </div>
        </div>
      )}

      <div>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-100">Running Instances</h2>
          <Link
            to="/admin/instances"
            className="text-sm font-medium text-emerald-400 hover:text-emerald-300"
          >
            Manage all →
          </Link>
        </div>
        {running.length === 0 ? (
          <div className="rounded-lg border border-gray-700 bg-gray-800 p-8 text-center text-sm text-gray-500">
            No instances are currently running.
          </div>
        ) : (
          <div className="apex-scroll overflow-x-auto rounded-lg border border-gray-700">
            <table className="min-w-full divide-y divide-gray-700 text-sm">
              <thead className="bg-gray-800">
                <tr className="text-left text-xs uppercase tracking-wider text-gray-400">
                  <th className="px-4 py-3">User</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3 text-right">PID</th>
                  <th className="px-4 py-3 text-right">Uptime</th>
                  <th className="px-4 py-3 text-right">Restarts</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-700">
                {running.map((inst, i) => (
                  <tr
                    key={inst.user_id}
                    className={i % 2 === 0 ? "bg-gray-800" : "bg-gray-750"}
                  >
                    <td className="px-4 py-3 text-gray-100">
                      {inst.email || `user #${inst.user_id}`}
                    </td>
                    <td className="px-4 py-3">
                      <StatusBadge status={inst.status} />
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums text-gray-300">
                      {inst.pid ?? "—"}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums text-gray-300">
                      {formatDuration(inst.uptime_seconds)}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums text-gray-300">
                      {inst.restarts ?? 0}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="flex flex-wrap gap-3">
        <Link
          to="/admin/users"
          className="rounded-md border border-gray-600 px-4 py-2 text-sm font-medium text-gray-300 transition-colors duration-200 hover:bg-gray-700"
        >
          Manage Users
        </Link>
        <Link
          to="/admin/trades"
          className="rounded-md border border-gray-600 px-4 py-2 text-sm font-medium text-gray-300 transition-colors duration-200 hover:bg-gray-700"
        >
          View All Trades
        </Link>
      </div>
    </div>
  );
}
