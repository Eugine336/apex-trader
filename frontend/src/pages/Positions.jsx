import { useCallback, useEffect, useState } from "react";

import { extractError } from "../api/client";
import { getPositions } from "../api/dashboard";
import { getStatus } from "../api/trading";
import PositionsTable from "../components/PositionsTable";
import StatusBadge from "../components/StatusBadge";

export default function Positions() {
  const [positions, setPositions] = useState([]);
  const [status, setStatus] = useState("STOPPED");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [lastUpdated, setLastUpdated] = useState(null);

  const load = useCallback(async () => {
    try {
      const [pos, st] = await Promise.all([getPositions(), getStatus()]);
      setPositions(pos);
      setStatus(st.status || "STOPPED");
      setLastUpdated(new Date());
      setError("");
    } catch (err) {
      setError(extractError(err, "Failed to load positions"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Auto-refresh every 5s only while the instance is running.
  useEffect(() => {
    if (status !== "RUNNING") return undefined;
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [status, load]);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-gray-100">Open Positions</h1>
        <div className="flex items-center gap-3">
          <StatusBadge status={status} />
          <button
            type="button"
            onClick={load}
            className="rounded-md border border-gray-600 px-3 py-2 text-sm font-medium text-gray-300 transition-colors duration-200 hover:bg-gray-700"
          >
            Refresh
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </div>
      )}

      {status === "RUNNING" && (
        <p className="text-xs text-gray-500">
          Auto-refreshing every 5s
          {lastUpdated ? ` · last updated ${lastUpdated.toLocaleTimeString()}` : ""}
        </p>
      )}

      {loading ? (
        <div className="animate-pulse text-sm text-gray-400">Loading positions…</div>
      ) : (
        <PositionsTable
          positions={positions}
          emptyMessage={
            status === "RUNNING"
              ? "No open positions right now."
              : "Instance is stopped — start it to manage positions."
          }
        />
      )}
    </div>
  );
}
