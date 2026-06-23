import { useMemo, useState } from "react";

import { getPlanner } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import SymbolFilter from "../../components/engine/SymbolFilter";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { badgeClass, dirText, fmtTs, truncate } from "../../utils/engineFormat";

const ACTION_BADGES = { ENTER: "green", WAIT: "yellow", SKIP: "muted" };

function actionBadge(action) {
  return ACTION_BADGES[(action || "").toUpperCase()] || "muted";
}

function fmtTsSeconds(secs) {
  if (!secs) return "—";
  try {
    return fmtTs(new Date(secs * 1000).toISOString());
  } catch {
    return "—";
  }
}

function pnlText(r) {
  if (r == null) return "text-gray-400";
  const n = Number(r);
  if (n > 0) return "text-emerald-400";
  if (n < 0) return "text-red-400";
  return "text-gray-400";
}

function StrategyCard({ title, buckets }) {
  const entries = Object.entries(buckets || {}).sort(
    (a, b) => (b[1]?.count || 0) - (a[1]?.count || 0)
  );
  return (
    <Panel title={title}>
      {entries.length === 0 ? (
        <div className="py-6 text-center text-xs text-gray-500">No plans yet.</div>
      ) : (
        <div className="flex flex-col gap-2">
          {entries.map(([key, b]) => {
            const completed = b.completed || 0;
            const wr = completed ? Math.round((b.win_rate || 0) * 100) : null;
            return (
              <div key={key} className="flex items-center gap-2">
                <span className="min-w-[110px] font-mono text-[11px] font-semibold text-gray-300">
                  {key}
                </span>
                <span className="min-w-[36px] text-right font-mono text-xs font-semibold text-gray-100">
                  {b.count || 0}
                </span>
                <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-700">
                  <div
                    className={`h-full ${
                      wr == null
                        ? "bg-gray-500"
                        : wr >= 50
                        ? "bg-emerald-500"
                        : "bg-red-500"
                    }`}
                    style={{ width: `${wr == null ? 0 : wr}%` }}
                  />
                </div>
                <span className="min-w-[86px] text-right font-mono text-[11px] font-semibold text-gray-300">
                  {wr == null ? `${completed} done` : `${wr}% (${completed})`}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

export default function Planner() {
  const fetcher = useMemo(() => () => getPlanner({ limit: 100 }), []);
  const { data, loading, error, unavailable } = useEnginePoll(fetcher, 5000);
  const [symbolFilter, setSymbolFilter] = useState("");

  const plans = data?.plans || [];
  const stats = data?.stats || {};
  const calib = data?.calibration || {};
  const thresholds = calib.thresholds || {};

  const filtered = useMemo(
    () =>
      plans.filter(
        (p) =>
          !symbolFilter ||
          (p.symbol || "").toUpperCase().includes(symbolFilter.toUpperCase())
      ),
    [plans, symbolFilter]
  );

  const completedRows = plans.filter((p) => p.outcome && p.outcome.pnl_r != null);
  const wins = completedRows.filter((p) => Number(p.outcome.pnl_r) > 0).length;
  const winRate = completedRows.length
    ? Math.round((wins / completedRows.length) * 100)
    : null;
  const avgR = completedRows.length
    ? completedRows.reduce((s, p) => s + Number(p.outcome.pnl_r || 0), 0) /
      completedRows.length
    : null;

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Trade Planner"
        subtitle="Plans coordinating every advisor, their outcomes, and self-calibration"
      />

      <EngineNotice
        loading={loading}
        unavailable={unavailable}
        error={error}
        hasData={!!data}
      />

      {data && (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <StatTile label="Total Plans" value={stats.total_plans || 0} />
            <StatTile label="Completed" value={stats.total_completed || 0} />
            <StatTile
              label="Win Rate"
              value={winRate == null ? "—" : `${winRate}%`}
              accent={
                winRate == null
                  ? "text-gray-100"
                  : winRate >= 50
                  ? "text-emerald-400"
                  : "text-red-400"
              }
            />
            <StatTile
              label="Avg R"
              value={
                avgR == null ? "—" : `${avgR >= 0 ? "+" : ""}${avgR.toFixed(2)}`
              }
              accent={pnlText(avgR)}
            />
          </div>

          <Panel title="Self-Calibration Status">
            <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
              <div>
                <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-gray-500">
                  Calibration
                </div>
                <span
                  className={badgeClass(
                    calib.calibration_enabled ? "green" : "muted"
                  )}
                >
                  {calib.calibration_enabled ? "ENABLED" : "DISABLED"}
                </span>
              </div>
              <div>
                <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-gray-500">
                  Completed Outcomes
                </div>
                <div className="font-mono text-base font-semibold text-gray-100">
                  {calib.completed_outcomes || 0}
                </div>
              </div>
              <div>
                <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-gray-500">
                  Next Calibration At
                </div>
                <div className="font-mono text-base font-semibold text-gray-100">
                  {calib.next_calibration_at || "—"}
                  <span className="ml-1.5 text-[11px] text-gray-500">
                    (
                    {calib.trades_until_next != null
                      ? `${calib.trades_until_next} to go`
                      : "—"}
                    )
                  </span>
                </div>
              </div>
              <div>
                <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-gray-500">
                  Last Calibrated
                </div>
                <div className="font-mono text-sm font-semibold text-gray-300">
                  {fmtTsSeconds(calib.last_calibrated_ts)}
                </div>
              </div>
            </div>

            {Object.keys(thresholds).length > 0 && (
              <div className="mt-4">
                <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-gray-500">
                  Live Planner Thresholds
                </div>
                <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
                  {Object.entries(thresholds).map(([k, v]) => (
                    <div
                      key={k}
                      className="flex justify-between gap-2 rounded bg-gray-900/60 px-2 py-1"
                    >
                      <span className="text-[10px] text-gray-500">{k}</span>
                      <span className="font-mono text-[11px] font-semibold text-gray-100">
                        {typeof v === "number" ? v : String(v)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </Panel>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <StrategyCard title="Entry Mode" buckets={stats.entry_mode_stats} />
            <StrategyCard title="SL Strategy" buckets={stats.sl_strategy_stats} />
            <StrategyCard title="TP Strategy" buckets={stats.tp_strategy_stats} />
          </div>

          <div className="flex items-center">
            <SymbolFilter value={symbolFilter} onChange={setSymbolFilter} />
          </div>

          <Panel title="Recent Trade Plans" className="overflow-x-auto">
            <table className="w-full min-w-[900px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="py-2 pr-2">Time</th>
                  <th className="py-2 pr-2">Symbol</th>
                  <th className="py-2 pr-2">Dir</th>
                  <th className="py-2 pr-2">Decision</th>
                  <th className="py-2 pr-2">Entry</th>
                  <th className="py-2 pr-2 text-right">Conv</th>
                  <th className="py-2 pr-2 text-right">Risk%</th>
                  <th className="py-2 pr-2 text-right">R</th>
                  <th className="py-2 pr-2">Reasoning</th>
                </tr>
              </thead>
              <tbody>
                {filtered.length === 0 && (
                  <tr>
                    <td colSpan={9} className="py-12 text-center text-gray-500">
                      {loading
                        ? "Loading…"
                        : "No plans recorded yet — plans appear here as the bot evaluates setups."}
                    </td>
                  </tr>
                )}
                {filtered.map((p, i) => {
                  const dir = p.direction || "";
                  const r = p.outcome?.pnl_r;
                  return (
                    <tr
                      key={p.plan_id ? `${p.plan_id}-${i}` : i}
                      className="border-b border-gray-800"
                    >
                      <td className="py-2 pr-2 text-xs text-gray-500">
                        {fmtTs(p.timestamp)}
                      </td>
                      <td className="py-2 pr-2 font-medium text-gray-100">
                        {p.symbol || "—"}
                      </td>
                      <td className={`py-2 pr-2 ${dirText(dir)}`}>{dir || "—"}</td>
                      <td className="py-2 pr-2">
                        <span className={badgeClass(actionBadge(p.action))}>
                          {p.action || "—"}
                        </span>
                      </td>
                      <td className="py-2 pr-2 text-xs text-gray-400">
                        {p.entry_mode || "—"}
                      </td>
                      <td className="py-2 pr-2 text-right font-mono text-xs">
                        {p.confidence != null ? Number(p.confidence).toFixed(2) : "—"}
                      </td>
                      <td className="py-2 pr-2 text-right font-mono text-xs">
                        {p.risk_pct != null ? Number(p.risk_pct).toFixed(2) : "—"}
                      </td>
                      <td
                        className={`py-2 pr-2 text-right font-mono text-xs font-semibold ${pnlText(
                          r
                        )}`}
                      >
                        {r != null
                          ? `${Number(r) >= 0 ? "+" : ""}${Number(r).toFixed(2)}`
                          : "—"}
                      </td>
                      <td className="py-2 pr-2 text-xs text-gray-500">
                        {truncate(p.reasoning, 70)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Panel>
        </>
      )}
    </div>
  );
}
