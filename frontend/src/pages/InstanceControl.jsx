import { useCallback, useEffect, useState } from "react";

import { extractError } from "../api/client";
import {
  getLogs,
  getStatus,
  restart as restartInstance,
  start as startInstance,
  stop as stopInstance,
} from "../api/trading";
import ConfirmDialog from "../components/ConfirmDialog";
import LogViewer from "../components/LogViewer";
import StatusBadge from "../components/StatusBadge";
import { formatDateTime } from "../utils/format";

function StatRow({ label, value }) {
  return (
    <div className="flex justify-between border-b border-gray-700/60 py-2 text-sm last:border-0">
      <span className="text-gray-400">{label}</span>
      <span className="font-medium text-gray-200">{value}</span>
    </div>
  );
}

export default function InstanceControl() {
  const [status, setStatus] = useState(null);
  const [logs, setLogs] = useState([]);
  const [error, setError] = useState("");
  const [actionLoading, setActionLoading] = useState(false);
  const [dialog, setDialog] = useState(null); // start | stop | restart

  const refreshStatus = useCallback(async () => {
    try {
      const s = await getStatus();
      setStatus(s);
      setError("");
    } catch (err) {
      setError(extractError(err, "Failed to load instance status"));
    }
  }, []);

  const refreshLogs = useCallback(async () => {
    try {
      const res = await getLogs(200);
      setLogs(res.lines || []);
    } catch {
      // Non-fatal — keep previous logs.
    }
  }, []);

  useEffect(() => {
    refreshStatus();
    refreshLogs();
  }, [refreshStatus, refreshLogs]);

  const isRunning = status?.status === "RUNNING";

  // Auto-refresh status (every 5s) + logs (every 3s) while running.
  useEffect(() => {
    if (!isRunning) return undefined;
    const statusId = setInterval(refreshStatus, 5000);
    const logId = setInterval(refreshLogs, 3000);
    return () => {
      clearInterval(statusId);
      clearInterval(logId);
    };
  }, [isRunning, refreshStatus, refreshLogs]);

  const runAction = async () => {
    setActionLoading(true);
    try {
      if (dialog === "start") await startInstance();
      else if (dialog === "stop") await stopInstance();
      else if (dialog === "restart") await restartInstance();
      await refreshStatus();
      await refreshLogs();
    } catch (err) {
      setError(extractError(err, "Action failed"));
    } finally {
      setActionLoading(false);
      setDialog(null);
    }
  };

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-gray-100">Instance Control</h1>

      {error && (
        <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="space-y-4 rounded-lg border border-gray-700 bg-gray-800 p-6 shadow-lg lg:col-span-1">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold text-gray-100">Status</h2>
            <StatusBadge status={status?.status} size="lg" />
          </div>

          <div>
            <StatRow label="State" value={status?.status || "—"} />
            <StatRow label="PID" value={status?.pid ?? "—"} />
            <StatRow label="Restarts" value={status?.restarts ?? 0} />
            <StatRow label="Started" value={formatDateTime(status?.started_at)} />
            <StatRow label="Stopped" value={formatDateTime(status?.stopped_at)} />
          </div>

          {status?.last_error && (
            <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-400">
              {status.last_error}
            </div>
          )}

          <div className="flex flex-wrap gap-2 pt-2">
            <button
              type="button"
              disabled={isRunning}
              onClick={() => setDialog("start")}
              className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-200 hover:bg-emerald-700 disabled:opacity-40"
            >
              Start
            </button>
            <button
              type="button"
              disabled={!isRunning}
              onClick={() => setDialog("stop")}
              className="rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-200 hover:bg-red-700 disabled:opacity-40"
            >
              Stop
            </button>
            <button
              type="button"
              onClick={() => setDialog("restart")}
              className="rounded-md border border-gray-600 px-4 py-2 text-sm font-medium text-gray-300 transition-colors duration-200 hover:bg-gray-700"
            >
              Restart
            </button>
          </div>
        </div>

        <div className="space-y-3 lg:col-span-2">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold text-gray-100">Live Logs</h2>
            <button
              type="button"
              onClick={refreshLogs}
              className="rounded-md border border-gray-600 px-3 py-1.5 text-sm font-medium text-gray-300 transition-colors duration-200 hover:bg-gray-700"
            >
              Refresh
            </button>
          </div>
          <LogViewer lines={logs} />
          {isRunning && (
            <p className="text-xs text-gray-500">Auto-refreshing every 3s</p>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={dialog !== null}
        title={
          dialog === "stop"
            ? "Stop instance?"
            : dialog === "restart"
            ? "Restart instance?"
            : "Start instance?"
        }
        message={
          dialog === "stop"
            ? "This stops your trading process. Open positions remain at the broker."
            : dialog === "restart"
            ? "This stops then restarts your trading process to apply any config changes."
            : "This starts your trading process using your stored broker credentials."
        }
        confirmLabel={
          dialog === "stop" ? "Stop" : dialog === "restart" ? "Restart" : "Start"
        }
        destructive={dialog === "stop"}
        loading={actionLoading}
        onConfirm={runAction}
        onCancel={() => setDialog(null)}
      />
    </div>
  );
}
