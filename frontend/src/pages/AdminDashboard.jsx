import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { getInstances, getStats } from "../api/admin";
import { extractError } from "../api/client";
import StatusBadge from "../components/StatusBadge";
import SummaryCard from "../components/SummaryCard";
import { formatDuration, formatMoney, pnlColor } from "../utils/format";

export default function AdminDashboard() {
  const [stats, setStats] = useState(null);
  const [instances, setInstances] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const [s, inst] = await Promise.all([getStats(), getInstances()]);
      setStats(s);
      setInstances(inst);
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
          label="Trades Today"
          value={stats?.trades_today ?? 0}
          sub={`${stats?.total_trades ?? 0} all-time`}
        />
        <SummaryCard
          label="Total P&L"
          value={formatMoney(stats?.total_pnl)}
          accent={pnlColor(stats?.total_pnl)}
          sub={`uptime ${formatDuration(stats?.system_uptime_seconds)}`}
        />
      </div>

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
