import { Fragment, useMemo, useState } from "react";

import { getPositionHealth } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import SymbolFilter from "../../components/engine/SymbolFilter";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { badgeClass, dirText } from "../../utils/engineFormat";

// Health → colour: green healthy, yellow tighten, sky moderate, red exit.
function healthHex(h) {
  if (h >= 0.8) return "#34d399";
  if (h >= 0.6) return "#facc15";
  if (h >= 0.4) return "#38bdf8";
  if (h >= 0.2) return "#f59e0b";
  return "#f87171";
}

function healthText(h) {
  if (h >= 0.8) return "text-emerald-400";
  if (h >= 0.6) return "text-yellow-400";
  if (h >= 0.4) return "text-sky-400";
  if (h >= 0.2) return "text-orange-400";
  return "text-red-400";
}

const ACTION_BADGES = {
  HOLD: "green",
  TIGHTEN_SL: "muted",
  SCALE_DOWN: "muted",
  EXIT_PARTIAL: "muted",
  EXIT_FULL: "red",
  SCALE_UP: "green",
};

function actionBadge(action) {
  return ACTION_BADGES[action] || "muted";
}

// Candidate-scoped thesis health (Session 4): the contributing panel that
// opened the position still supports it (intact), has flipped against it, or
// went silent. Empty when no candidate provenance was captured.
const THESIS_BADGES = {
  intact: "green",
  flipped: "red",
  silent: "orange",
};

function thesisBadge(status) {
  return THESIS_BADGES[String(status || "").toLowerCase()] || "muted";
}

function DimBar({ d }) {
  const m = d.multiplier ?? d.avg_multiplier ?? 0;
  const pct = Math.round(m * 100);
  return (
    <div className="mb-1.5">
      <div className="flex justify-between text-[11px]">
        <span className="text-gray-400">{d.name}</span>
        <span className="font-mono" style={{ color: healthHex(m) }}>
          ×{Number(m).toFixed(2)}
        </span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-gray-700">
        <div
          className="h-full"
          style={{ width: `${pct}%`, background: healthHex(m) }}
        />
      </div>
      {d.reason && (
        <div className="mt-0.5 text-[10px] text-gray-500">{d.reason}</div>
      )}
    </div>
  );
}

// Normalize a health report's dimension breakdown for <DimBar>. The
// orchestrator emitter ships a `dimensions` array, while the live
// decision-engine management emitter ships a `dimension_scores` dict
// ({tf_alignment, momentum, structure_integrity}); accept both.
function dimsFromReport(r) {
  if (Array.isArray(r.dimensions) && r.dimensions.length) return r.dimensions;
  const scores = r.dimension_scores;
  if (scores && typeof scores === "object") {
    return Object.entries(scores).map(([name, multiplier]) => ({
      name: name.replace(/_/g, " "),
      multiplier,
    }));
  }
  return [];
}

// Thesis-change notes: the orchestrator emitter ships a `thesis_changes`
// array; the live management emitter ships a scalar `reason` — fall back to it.
function changesFromReport(r) {
  if (Array.isArray(r.thesis_changes) && r.thesis_changes.length) {
    return r.thesis_changes;
  }
  if (r.reason) return [r.reason];
  return [];
}

// Compact health-over-time sparkline (no chart-lib dependency).
function Spark({ series }) {
  const pts = (series || []).map((s) => s.health_score ?? 0);
  if (pts.length === 0) return <span className="text-gray-500">—</span>;
  const w = 120;
  const h = 22;
  const step = pts.length > 1 ? w / (pts.length - 1) : w;
  const path = pts
    .map(
      (v, i) => `${i === 0 ? "M" : "L"} ${(i * step).toFixed(1)} ${(h - v * h).toFixed(1)}`
    )
    .join(" ");
  const last = pts[pts.length - 1];
  return (
    <svg width={w} height={h} className="block">
      <line
        x1="0"
        y1={h - 0.8 * h}
        x2={w}
        y2={h - 0.8 * h}
        stroke="#374151"
        strokeDasharray="2 2"
      />
      <path d={path} fill="none" stroke={healthHex(last)} strokeWidth="1.5" />
    </svg>
  );
}

export default function PositionHealth() {
  const fetcher = useMemo(() => () => getPositionHealth({ limit: 200 }), []);
  const { data, loading, error, unavailable } = useEnginePoll(fetcher, 5000);
  const [expanded, setExpanded] = useState(null);
  const [symbolFilter, setSymbolFilter] = useState("");

  const positions = data?.positions || [];
  const reports = data?.reports || [];
  const stats = data?.stats || {};
  const cfg = data?.config || {};

  const filteredPos = useMemo(() => {
    if (!symbolFilter) return positions;
    const q = symbolFilter.toUpperCase();
    return positions.filter((p) => (p.pair || "").toUpperCase().includes(q));
  }, [positions, symbolFilter]);

  const actionLog = useMemo(() => {
    let rows = reports.filter((r) => r.action && r.action !== "HOLD");
    if (symbolFilter) {
      const q = symbolFilter.toUpperCase();
      rows = rows.filter((r) => (r.pair || "").toUpperCase().includes(q));
    }
    return rows.slice(-50).reverse();
  }, [reports, symbolFilter]);

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Position Health"
        subtitle="The round table for OPEN trades — each cycle every position's evidence is regraded into a health score and a bounded management action"
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
            <StatTile
              label="Mode"
              value={cfg.manage_open_positions ? "LIVE MANAGE" : "LEGACY"}
              accent={
                cfg.manage_open_positions ? "text-emerald-400" : "text-yellow-400"
              }
            />
            <StatTile label="Open Positions" value={positions.length} />
            <StatTile
              label="Avg Health"
              value={Number(stats.avg_health || 0).toFixed(2)}
              accent={healthText(stats.avg_health || 0)}
            />
            <StatTile label="Scale-Up" value={cfg.allow_scale_up ? "ON" : "OFF"} />
          </div>

          <Panel title="Dimensions dragging health down most (avg multiplier)">
            {(stats.dimensions || []).length === 0 ? (
              <div className="py-4 text-center text-xs text-gray-500">
                No evaluations yet.
              </div>
            ) : (
              (stats.dimensions || []).map((d) => <DimBar key={d.name} d={d} />)
            )}
          </Panel>

          <div className="flex items-center">
            <SymbolFilter value={symbolFilter} onChange={setSymbolFilter} />
          </div>

          <Panel
            title="Open positions"
            subtitle="Click a row to see the dimension breakdown + entry vs now"
            className="overflow-x-auto"
          >
            <table className="w-full min-w-[680px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="py-2 pr-2">Symbol</th>
                  <th className="py-2 pr-2">Dir</th>
                  <th className="py-2 pr-2">Horizon</th>
                  <th className="py-2 pr-2">Thesis</th>
                  <th className="py-2 pr-2 text-right">Health</th>
                  <th className="py-2 pr-2">Action</th>
                  <th className="py-2 pr-2 text-right">P&L (R)</th>
                  <th className="py-2 pr-2">Trend</th>
                </tr>
              </thead>
              <tbody>
                {filteredPos.length === 0 && (
                  <tr>
                    <td colSpan={8} className="py-12 text-center text-gray-500">
                      {loading
                        ? "Loading…"
                        : "No open-position health evaluations yet — they appear as positions are managed."}
                    </td>
                  </tr>
                )}
                {filteredPos.map((p, idx) => {
                  const key = `${p.order_id || p.pair}-${idx}`;
                  const isOpen = expanded === key;
                  const latest =
                    reports
                      .filter(
                        (r) =>
                          String(r.order_id || r.pair) ===
                          String(p.order_id || p.pair)
                      )
                      .slice(-1)[0] || {};
                  return (
                    <Fragment key={key}>
                      <tr
                        className="cursor-pointer border-b border-gray-800 hover:bg-gray-700/30"
                        onClick={() => setExpanded(isOpen ? null : key)}
                      >
                        <td className="py-2 pr-2 font-semibold text-gray-100">
                          {p.pair}
                        </td>
                        <td className={`py-2 pr-2 ${dirText(p.direction)}`}>
                          {p.direction}
                        </td>
                        <td className="py-2 pr-2 text-gray-400">
                          {p.horizon || p.timeframe_class || "—"}
                        </td>
                        <td className="py-2 pr-2">
                          {p.thesis_status ? (
                            <span className={badgeClass(thesisBadge(p.thesis_status))}>
                              {p.thesis_status}
                            </span>
                          ) : (
                            <span className="text-xs text-gray-600">—</span>
                          )}
                        </td>
                        <td
                          className="py-2 pr-2 text-right font-mono font-bold"
                          style={{ color: healthHex(p.health_score) }}
                        >
                          {Number(p.health_score || 0).toFixed(2)}
                        </td>
                        <td className="py-2 pr-2">
                          <span className={badgeClass(actionBadge(p.action))}>
                            {p.action || "—"}
                          </span>
                        </td>
                        <td
                          className={`py-2 pr-2 text-right font-mono ${
                            (p.profit_r || 0) >= 0
                              ? "text-emerald-400"
                              : "text-red-400"
                          }`}
                        >
                          {Number(p.profit_r || 0).toFixed(2)}
                        </td>
                        <td className="py-2 pr-2">
                          <Spark series={p.series} />
                        </td>
                      </tr>
                      {isOpen && (
                        <tr>
                          <td colSpan={8} className="bg-gray-900/40">
                            <div className="p-3">
                              {(p.candidate_id ||
                                (p.contributing_modules || []).length > 0) && (
                                <div className="mb-2 text-[11px] text-gray-400">
                                  Managed by candidate{" "}
                                  <span className="font-mono text-gray-300">
                                    {p.candidate_id || "—"}
                                  </span>
                                  {p.timeframe_class ? ` · ${p.timeframe_class}` : ""}
                                  {(p.contributing_modules || []).length
                                    ? ` · panel: ${p.contributing_modules.join(", ")}`
                                    : ""}
                                </div>
                              )}
                              {(() => {
                                const dims = dimsFromReport(latest);
                                const changes = changesFromReport(latest);
                                return (
                                  <>
                                    {latest.entry_health_at_open != null && (
                                      <div className="mb-2 text-xs text-gray-400">
                                        Entry health{" "}
                                        <b>{Number(latest.entry_health_at_open).toFixed(2)}</b>{" "}
                                        → now{" "}
                                        <b style={{ color: healthHex(latest.health_score) }}>
                                          {Number(latest.health_score || 0).toFixed(2)}
                                        </b>{" "}
                                        (Δ {Number(latest.health_delta || 0).toFixed(2)})
                                      </div>
                                    )}
                                    {dims.map((d, i) => (
                                      <DimBar key={i} d={d} />
                                    ))}
                                    {changes.length > 0 && (
                                      <div className="mt-1.5 text-[11px] text-gray-500">
                                        Changes since entry: {changes.join("; ")}
                                      </div>
                                    )}
                                    {dims.length === 0 && (
                                      <div className="text-xs text-gray-500">
                                        No dimension detail recorded.
                                      </div>
                                    )}
                                  </>
                                );
                              })()}
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </Panel>

          <Panel title="Management action log (non-HOLD)" className="overflow-x-auto">
            <table className="w-full min-w-[680px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="py-2 pr-2">Time</th>
                  <th className="py-2 pr-2">Symbol</th>
                  <th className="py-2 pr-2 text-right">Health</th>
                  <th className="py-2 pr-2">Action</th>
                  <th className="py-2 pr-2">Changes</th>
                </tr>
              </thead>
              <tbody>
                {actionLog.length === 0 && (
                  <tr>
                    <td colSpan={5} className="py-8 text-center text-gray-500">
                      No management actions yet — healthy positions HOLD.
                    </td>
                  </tr>
                )}
                {actionLog.map((r, i) => (
                  <tr key={i} className="border-b border-gray-800">
                    <td className="py-2 pr-2 text-xs text-gray-500">
                      {(r._event_ts || "").replace("T", " ").slice(0, 19)}
                    </td>
                    <td className="py-2 pr-2 font-semibold text-gray-100">
                      {r.pair}
                    </td>
                    <td
                      className="py-2 pr-2 text-right font-mono"
                      style={{ color: healthHex(r.health_score) }}
                    >
                      {Number(r.health_score || 0).toFixed(2)}
                    </td>
                    <td className="py-2 pr-2">
                      <span className={badgeClass(actionBadge(r.action))}>
                        {r.action}
                      </span>
                    </td>
                    <td className="py-2 pr-2 text-xs text-gray-400">
                      {changesFromReport(r).join("; ") || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>
        </>
      )}
    </div>
  );
}
