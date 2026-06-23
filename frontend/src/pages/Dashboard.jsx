import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { extractError } from "../api/client";
import { getEquityCurve, getHistory, getSummary } from "../api/dashboard";
import { start as startInstance, stop as stopInstance } from "../api/trading";
import ConfirmDialog from "../components/ConfirmDialog";
import EquityChart from "../components/EquityChart";
import StatusBadge from "../components/StatusBadge";
import SummaryCard from "../components/SummaryCard";
import TradesTable from "../components/TradesTable";
import { formatMoney, formatPercent, pnlColor } from "../utils/format";

const RUNNING_STATES = new Set(["RUNNING", "STARTING"]);

export default function Dashboard() {
  const [summary, setSummary] = useState(null);
  const [equity, setEquity] = useState([]);
  const [recent, setRecent] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionLoading, setActionLoading] = useState(false);
  const [dialog, setDialog] = useState(null); // "start" | "stop" | null

  const load = useCallback(async () => {
    try {
      const [s, e, h] = await Promise.all([
        getSummary(),
        getEquityCurve(500),
        getHistory({ limit: 10 }),
      ]);
      setSummary(s);
      setEquity(e);
      setRecent(h);
      setError("");
    } catch (err) {
      setError(extractError(err, "Failed to load dashboard"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, [load]);

  const runConfirmedAction = async () => {
    setActionLoading(true);
    try {
      if (dialog === "start") await startInstance();
      else if (dialog === "stop") await stopInstance();
      await load();
    } catch (err) {
      setError(extractError(err, "Action failed"));
    } finally {
      setActionLoading(false);
      setDialog(null);
    }
  };

  const isRunning = summary ? RUNNING_STATES.has(summary.instance_status) : false;

  if (loading) {
    return <div className="animate-pulse text-sm text-gray-400">Loading dashboard…</div>;
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-gray-100">Dashboard</h1>
        <div className="flex items-center gap-3">
          <StatusBadge status={summary?.instance_status} />
          {isRunning ? (
            <button
              type="button"
              onClick={() => setDialog("stop")}
              className="rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-200 hover:bg-red-700"
            >
              Stop
            </button>
          ) : (
            <button
              type="button"
              onClick={() => setDialog("start")}
              className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-200 hover:bg-emerald-700"
            >
              Start
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <SummaryCard
          label="Total P&L"
          value={formatMoney(summary?.total_pnl)}
          accent={pnlColor(summary?.total_pnl)}
        />
        <SummaryCard
          label="Win Rate"
          value={formatPercent(summary?.win_rate)}
          sub={`${summary?.wins ?? 0}W / ${summary?.losses ?? 0}L`}
        />
        <SummaryCard label="Total Trades" value={summary?.total_trades ?? 0} />
        <SummaryCard
          label="Instance"
          value={<StatusBadge status={summary?.instance_status} size="lg" />}
        />
      </div>

      <div className="rounded-lg border border-gray-700 bg-gray-800 p-5 shadow-lg">
        <h2 className="mb-4 text-lg font-semibold text-gray-100">Equity Curve</h2>
        <EquityChart data={equity} />
      </div>

      <div>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-100">Recent Trades</h2>
          <Link
            to="/trades"
            className="text-sm font-medium text-emerald-400 hover:text-emerald-300"
          >
            View all →
          </Link>
        </div>
        <TradesTable trades={recent} emptyMessage="No trades closed yet." />
      </div>

      <ConfirmDialog
        open={dialog !== null}
        title={dialog === "stop" ? "Stop instance?" : "Start instance?"}
        message={
          dialog === "stop"
            ? "This will stop your trading instance. Open positions remain at the broker."
            : "This will start your trading instance using your stored broker credentials."
        }
        confirmLabel={dialog === "stop" ? "Stop" : "Start"}
        destructive={dialog === "stop"}
        loading={actionLoading}
        onConfirm={runConfirmedAction}
        onCancel={() => setDialog(null)}
      />
    </div>
  );
}
