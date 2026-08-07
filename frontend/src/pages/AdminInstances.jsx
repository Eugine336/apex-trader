import { useCallback, useEffect, useState } from "react";

import {
  getInstances,
  startInstance,
  stopInstance,
} from "../api/admin";
import { extractError } from "../api/client";
import ConfirmDialog from "../components/ConfirmDialog";
import StatusBadge from "../components/StatusBadge";
import { formatDuration } from "../utils/format";

export default function AdminInstances() {
  const [instances, setInstances] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionLoading, setActionLoading] = useState(false);
  // pending = { userId, action: "start"|"stop", email } | null
  const [pending, setPending] = useState(null);

  const load = useCallback(async () => {
    try {
      const rows = await getInstances();
      setInstances(rows);
      setError("");
    } catch (err) {
      setError(extractError(err, "Failed to load instances"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, [load]);

  const runAction = async () => {
    if (!pending) return;
    setActionLoading(true);
    try {
      if (pending.action === "start") await startInstance(pending.userId);
      else await stopInstance(pending.userId);
      await load();
    } catch (err) {
      setError(extractError(err, "Action failed"));
    } finally {
      setActionLoading(false);
      setPending(null);
    }
  };

  if (loading) {
    return <div className="animate-pulse text-sm text-gray-400">Loading instances…</div>;
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-gray-100">Instance Monitor</h1>

      {error && (
        <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </div>
      )}

      {instances.length === 0 ? (
        <div className="rounded-lg border border-gray-700 bg-gray-800 p-8 text-center text-sm text-gray-500">
          No trading instances yet.
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
                <th className="px-4 py-3">Last Error</th>
                <th className="px-4 py-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-700">
              {instances.map((inst, i) => (
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
                  <td
                    className="max-w-xs truncate px-4 py-3 text-gray-400"
                    title={inst.last_error || ""}
                  >
                    {inst.last_error || "—"}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex justify-end gap-2">
                      {inst.alive ? (
                        <button
                          type="button"
                          disabled={actionLoading}
                          onClick={() =>
                            setPending({
                              userId: inst.user_id,
                              action: "stop",
                              email: inst.email,
                            })
                          }
                          className="rounded-md bg-red-600 px-3 py-1.5 text-xs font-medium text-white transition-colors duration-200 hover:bg-red-700 disabled:opacity-50"
                        >
                          Stop
                        </button>
                      ) : (
                        <button
                          type="button"
                          disabled={actionLoading}
                          onClick={() =>
                            setPending({
                              userId: inst.user_id,
                              action: "start",
                              email: inst.email,
                            })
                          }
                          className="rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white transition-colors duration-200 hover:bg-emerald-700 disabled:opacity-50"
                        >
                          Start
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <ConfirmDialog
        open={pending !== null}
        title={
          pending?.action === "stop" ? "Stop instance?" : "Start instance?"
        }
        message={
          pending?.action === "stop"
            ? `Force-stop the trading instance for ${pending?.email || "this user"}. Open positions remain at the broker.`
            : `Start the trading instance for ${pending?.email || "this user"} using their stored broker credentials.`
        }
        confirmLabel={pending?.action === "stop" ? "Stop" : "Start"}
        destructive={pending?.action === "stop"}
        loading={actionLoading}
        onConfirm={runAction}
        onCancel={() => setPending(null)}
      />
    </div>
  );
}
