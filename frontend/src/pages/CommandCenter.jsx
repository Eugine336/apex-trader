import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { extractError } from "../api/client";
import { getEquityCurve, getHistory } from "../api/dashboard";
import { start as startInstance, stop as stopInstance } from "../api/trading";
import ConfirmDialog from "../components/ConfirmDialog";
import EquityChart from "../components/EquityChart";
import LiveValue from "../components/LiveValue";
import PositionsTable from "../components/PositionsTable";
import StatusBadge from "../components/StatusBadge";
import TradesTable from "../components/TradesTable";
import { useRealtime } from "../context/RealtimeContext";
import {
  formatDuration,
  formatMoney,
  formatPercent,
  pnlColor,
} from "../utils/format";

const RUNNING_STATES = new Set(["RUNNING", "STARTING"]);

// Department → representative route for the quick-link grid.
const DEPT_ROUTE = {
  intelligence: "/engine/scanner",
  consensus: "/engine/ranker",
  compliance: "/engine/risk",
  portfolio: "/engine/planner",
  execution: "/engine/active-trades",
  operations: "/engine/operations",
  learning: "/engine/learning",
  governance: "/engine/module-governor",
  command: "/settings/instance",
};

const STATE_STYLE = {
  active: { dot: "bg-emerald-500", text: "text-emerald-400", ring: "ring-emerald-500/20" },
  degraded: { dot: "bg-amber-400", text: "text-amber-300", ring: "ring-amber-500/20" },
  error: { dot: "bg-red-500", text: "text-red-400", ring: "ring-red-500/25" },
  offline: { dot: "bg-gray-600", text: "text-gray-500", ring: "ring-gray-700/40" },
};

function MetricTile({ label, value, raw, accent = "text-gray-100", sub = null }) {
  return (
    <div className="rounded-lg border border-gray-700/80 bg-gray-800/80 px-4 py-3.5">
      <p className="text-[11px] font-medium uppercase tracking-[0.08em] text-gray-400">
        {label}
      </p>
      <LiveValue
        value={raw}
        format={() => value}
        className={`mt-2 block text-2xl font-semibold leading-none ${accent}`}
      />
      {sub && <p className="mt-1.5 text-[11px] text-gray-500">{sub}</p>}
    </div>
  );
}

function DepartmentCard({ dept }) {
  const style = STATE_STYLE[dept.state] || STATE_STYLE.offline;
  const route = DEPT_ROUTE[dept.key] || "/";
  return (
    <Link
      to={route}
      className={`group flex flex-col justify-between rounded-lg border border-gray-700/70 bg-gray-800/70 p-3 ring-1 ring-inset ${style.ring} transition-colors hover:border-gray-600 hover:bg-gray-800`}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="text-[13px] font-semibold text-gray-200 group-hover:text-gray-100">
          {dept.name}
        </span>
        <span className={`mt-1 h-2 w-2 shrink-0 rounded-full ${style.dot}`} />
      </div>
      <div className="mt-3 flex items-center justify-between">
        <span
          className={`text-[10px] font-semibold uppercase tracking-[0.08em] ${style.text}`}
        >
          {dept.state}
        </span>
        {dept.detail && (
          <span className="truncate text-[10px] text-gray-500">{dept.detail}</span>
        )}
      </div>
    </Link>
  );
}

export default function CommandCenter() {
  const { snapshot, connected, transport } = useRealtime();
  const [equity, setEquity] = useState([]);
  const [recent, setRecent] = useState([]);
  const [error, setError] = useState("");
  const [dialog, setDialog] = useState(null); // "start" | "stop" | null
  const [actionLoading, setActionLoading] = useState(false);

  // Charts + activity are lower-frequency than the live snapshot — poll slowly.
  const loadSlow = useCallback(async () => {
    try {
      const [e, h] = await Promise.all([
        getEquityCurve(500),
        getHistory({ limit: 12 }),
      ]);
      setEquity(e);
      setRecent(h);
      setError("");
    } catch (err) {
      setError(extractError(err, "Failed to load history"));
    }
  }, []);

  useEffect(() => {
    loadSlow();
    const id = setInterval(loadSlow, 20000);
    return () => clearInterval(id);
  }, [loadSlow]);

  const status = snapshot?.instance?.status || "STOPPED";
  const isRunning = RUNNING_STATES.has(String(status).toUpperCase());
  const summary = snapshot?.summary || {};
  const departments = snapshot?.departments || [];
  const positions = snapshot?.positions || [];
  const uptime = snapshot?.instance?.uptime_seconds || 0;

  const runConfirmedAction = async () => {
    setActionLoading(true);
    try {
      if (dialog === "start") await startInstance();
      else if (dialog === "stop") await stopInstance();
      await loadSlow();
    } catch (err) {
      setError(extractError(err, "Action failed"));
    } finally {
      setActionLoading(false);
      setDialog(null);
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-gray-100">
            Command Center
          </h1>
          <p className="mt-0.5 text-sm text-gray-400">
            Live system overview across all nine departments
          </p>
        </div>
        <div className="flex items-center gap-3">
          <StatusBadge status={status} size="lg" />
          {isRunning ? (
            <button
              type="button"
              onClick={() => setDialog("stop")}
              className="rounded-md bg-red-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-red-700"
            >
              Stop
            </button>
          ) : (
            <button
              type="button"
              onClick={() => setDialog("start")}
              className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-emerald-700"
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

      {!snapshot && (
        <div className="flex items-center gap-2 text-sm text-gray-400">
          <span className="h-2 w-2 animate-pulse rounded-full bg-accent" />
          Connecting to live feed…
        </div>
      )}

      {/* Key metrics */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <MetricTile
          label="Total P&L"
          raw={summary.total_pnl ?? 0}
          value={formatMoney(summary.total_pnl)}
          accent={pnlColor(summary.total_pnl)}
        />
        <MetricTile
          label="Open P&L"
          raw={snapshot?.open_pnl ?? 0}
          value={formatMoney(snapshot?.open_pnl)}
          accent={pnlColor(snapshot?.open_pnl)}
        />
        <MetricTile
          label="Open Positions"
          raw={snapshot?.open_positions_count ?? 0}
          value={snapshot?.open_positions_count ?? 0}
        />
        <MetricTile
          label="Win Rate"
          raw={summary.win_rate ?? 0}
          value={formatPercent(summary.win_rate)}
          sub={`${summary.wins ?? 0}W / ${summary.losses ?? 0}L`}
        />
        <MetricTile
          label="Max Drawdown"
          raw={summary.max_drawdown ?? 0}
          value={formatMoney(summary.max_drawdown)}
          accent={summary.max_drawdown ? "text-red-400" : "text-gray-100"}
        />
        <MetricTile
          label="Uptime"
          raw={uptime}
          value={isRunning ? formatDuration(uptime) : "—"}
          sub={transport === "sse" ? "live stream" : connected ? "polling" : "offline"}
        />
      </div>

      {/* Department health grid */}
      <div>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.1em] text-gray-400">
            Departments
          </h2>
          <span className="text-[11px] text-gray-500">
            {departments.filter((d) => d.state === "active").length}/
            {departments.length || 9} active
          </span>
        </div>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-3 xl:grid-cols-5">
          {(departments.length
            ? departments
            : Array.from({ length: 9 }).map((_, i) => ({
                key: `ph-${i}`,
                name: "—",
                state: "offline",
                detail: "",
              }))
          ).map((d) => (
            <DepartmentCard key={d.key} dept={d} />
          ))}
        </div>
      </div>

      {/* Equity + positions */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="rounded-lg border border-gray-700/80 bg-gray-800/80 xl:col-span-2">
          <div className="flex items-center justify-between border-b border-gray-700/70 px-4 py-3">
            <h2 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-gray-300">
              Equity Curve
            </h2>
            <Link
              to="/trades"
              className="text-xs font-medium text-accent-soft hover:text-accent"
            >
              History →
            </Link>
          </div>
          <div className="p-4">
            <EquityChart data={equity} />
          </div>
        </div>

        <div className="rounded-lg border border-gray-700/80 bg-gray-800/80">
          <div className="flex items-center justify-between border-b border-gray-700/70 px-4 py-3">
            <h2 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-gray-300">
              Open Positions
            </h2>
            <Link
              to="/positions"
              className="text-xs font-medium text-accent-soft hover:text-accent"
            >
              All →
            </Link>
          </div>
          <div className="p-4">
            <PositionsTable
              positions={positions}
              emptyMessage={
                isRunning
                  ? "No open positions right now."
                  : "Instance stopped — start it to open positions."
              }
            />
          </div>
        </div>
      </div>

      {/* Recent activity */}
      <div>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.1em] text-gray-400">
            Recent Activity
          </h2>
          <Link
            to="/trades"
            className="text-xs font-medium text-accent-soft hover:text-accent"
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
