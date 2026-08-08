import { useMemo, useState } from "react";

import { getDecisions } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import SymbolFilter from "../../components/engine/SymbolFilter";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import {
  actionBadgeVariant,
  badgeClass,
  dimBarPct,
  dimBgHex,
  dimText,
  dirText,
  fmtSigned,
  fmtTs,
  truncate,
} from "../../utils/engineFormat";

const TYPE_FILTERS = ["ALL", "MANAGEMENT", "ENTRY"];

const DIMS = [
  { key: "avg_tf_alignment", label: "TF Alignment", min: -1, max: 1 },
  { key: "avg_momentum", label: "Momentum", min: -1, max: 1 },
  { key: "avg_structure_integrity", label: "Structure", min: 0, max: 1 },
  { key: "avg_read_confidence", label: "Confidence", min: 0, max: 1 },
];

function DistRow({ label, count, total, barClass, labelEl }) {
  const pct = total ? ((count / total) * 100).toFixed(0) : "0";
  return (
    <div className="flex items-center gap-2">
      {labelEl || (
        <span className="min-w-[160px] font-mono text-xs font-semibold text-gray-300">
          {label}
        </span>
      )}
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-700">
        <div className={`h-full ${barClass}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="min-w-[60px] text-right font-mono text-xs font-semibold text-gray-300">
        {count} ({pct}%)
      </span>
    </div>
  );
}

export default function Decisions() {
  const fetcher = useMemo(() => () => getDecisions({ limit: 100 }), []);
  const { data, loading, error, unavailable } = useEnginePoll(fetcher, 5000);
  const [typeFilter, setTypeFilter] = useState("ALL");
  const [symbolFilter, setSymbolFilter] = useState("");

  const decisions = data?.decisions || [];
  const stats = data?.stats || {};
  const dims = stats.dimensions || {};
  const actionCounts = stats.action_counts || {};
  const sitCounts = stats.situation_counts || {};
  const total = stats.total_decisions || 1;

  const filtered = useMemo(() => {
    return decisions.filter((d) => {
      const dt = (d.decision_type || "MANAGEMENT").toUpperCase();
      const matchType = typeFilter === "ALL" || dt === typeFilter;
      const matchSym =
        !symbolFilter || (d.symbol || "").toUpperCase().includes(symbolFilter.toUpperCase());
      return matchType && matchSym;
    });
  }, [decisions, typeFilter, symbolFilter]);

  const actionEntries = Object.entries(actionCounts).sort((a, b) => b[1] - a[1]);
  const sitEntries = Object.entries(sitCounts).sort((a, b) => b[1] - a[1]);

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Decision Intelligence"
        subtitle="Situation assessments, contextual decisions, and governor oversight"
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
            <StatTile label="Total Decisions" value={stats.total_decisions || 0} />
            <StatTile
              label="Governor Vetoes"
              value={stats.governor_vetoes || 0}
              accent={(stats.governor_vetoes || 0) > 0 ? "text-red-400" : "text-gray-100"}
            />
            <StatTile
              label="Governor Changes"
              value={stats.governor_changes || 0}
              accent={(stats.governor_changes || 0) > 0 ? "text-yellow-400" : "text-gray-100"}
            />
            <StatTile label="Unique Actions" value={actionEntries.length} />
          </div>

          {Object.keys(dims).length > 0 && (
            <Panel title="Average Situation Dimensions">
              <div className="grid grid-cols-2 gap-6 lg:grid-cols-4">
                {DIMS.map((d) => {
                  const v = dims[d.key];
                  return (
                    <div key={d.key} className="text-center">
                      <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-gray-500">
                        {d.label}
                      </div>
                      <div className="h-2 overflow-hidden rounded-full bg-gray-700">
                        <div
                          className="h-full"
                          style={{
                            width: `${dimBarPct(v, d.min, d.max)}%`,
                            background: dimBgHex(v, d.min),
                          }}
                        />
                      </div>
                      <div className={`mt-1.5 font-mono text-lg font-semibold ${dimText(v, d.min)}`}>
                        {fmtSigned(v)}
                      </div>
                    </div>
                  );
                })}
              </div>
            </Panel>
          )}

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <Panel title="Action Distribution">
              {actionEntries.length === 0 ? (
                <div className="py-6 text-center text-xs text-gray-500">
                  No decisions recorded yet.
                </div>
              ) : (
                <div className="flex flex-col gap-1.5">
                  {actionEntries.map(([action, count]) => (
                    <DistRow
                      key={action}
                      count={count}
                      total={total}
                      barClass="bg-sky-500"
                      labelEl={
                        <span
                          className={`${badgeClass(
                            actionBadgeVariant(action)
                          )} min-w-[120px] text-center`}
                        >
                          {action}
                        </span>
                      }
                    />
                  ))}
                </div>
              )}
            </Panel>

            <Panel title="Situation Labels">
              {sitEntries.length === 0 ? (
                <div className="py-6 text-center text-xs text-gray-500">
                  No situations classified yet.
                </div>
              ) : (
                <div className="flex flex-col gap-1.5">
                  {sitEntries.map(([label, count]) => (
                    <DistRow key={label} label={label} count={count} total={total} barClass="bg-purple-500" />
                  ))}
                </div>
              )}
            </Panel>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {TYPE_FILTERS.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setTypeFilter(t)}
                className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                  typeFilter === t
                    ? "bg-emerald-600/20 text-emerald-400"
                    : "bg-gray-800 text-gray-400 hover:bg-gray-700/60 hover:text-gray-100"
                }`}
              >
                {t}
              </button>
            ))}
            <div className="ml-auto">
              <SymbolFilter value={symbolFilter} onChange={setSymbolFilter} />
            </div>
          </div>

          <Panel title="Recent Decisions" className="overflow-x-auto">
            <table className="w-full min-w-[900px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="py-2 pr-2">Time</th>
                  <th className="py-2 pr-2">Symbol</th>
                  <th className="py-2 pr-2">Dir</th>
                  <th className="py-2 pr-2">Type</th>
                  <th className="py-2 pr-2">Action</th>
                  <th className="py-2 pr-2">Situation</th>
                  <th className="py-2 pr-2 text-right">Align</th>
                  <th className="py-2 pr-2 text-right">Score</th>
                  <th className="py-2 pr-2 text-right">PnL</th>
                  <th className="py-2 pr-2">Gov</th>
                  <th className="py-2 pr-2">Reason</th>
                </tr>
              </thead>
              <tbody>
                {filtered.length === 0 && (
                  <tr>
                    <td colSpan={11} className="py-12 text-center text-gray-500">
                      No decisions recorded yet — they appear here as the bot runs.
                    </td>
                  </tr>
                )}
                {filtered.map((d, i) => {
                  const dec = d.decision || {};
                  const sit = d.situation || {};
                  const dir = d.direction || "";
                  const dt = (d.decision_type || "MGMT").slice(0, 4).toUpperCase();
                  const pnlPips = d.pnl_pips;
                  const govChanged = dec.governor_changed || dec.governor_vetoed;
                  return (
                    <tr key={d.order_id ? `${d.order_id}-${i}` : i} className="border-b border-gray-800">
                      <td className="py-2 pr-2 text-xs text-gray-500">{fmtTs(d.timestamp)}</td>
                      <td className="py-2 pr-2 font-medium text-gray-100">{d.symbol || "—"}</td>
                      <td className={`py-2 pr-2 ${dirText(dir)}`}>{dir || "—"}</td>
                      <td className="py-2 pr-2">
                        <span className={badgeClass(dt === "ENTR" ? "blue" : "muted")}>{dt}</span>
                      </td>
                      <td className="py-2 pr-2">
                        <span className={badgeClass(actionBadgeVariant(dec.action))}>
                          {dec.action || "—"}
                        </span>
                      </td>
                      <td className="py-2 pr-2 text-xs text-gray-400">{sit.label || "—"}</td>
                      <td className={`py-2 pr-2 text-right ${dimText(sit.tf_alignment, -1)}`}>
                        {sit.tf_alignment != null ? fmtSigned(sit.tf_alignment) : "—"}
                      </td>
                      <td className="py-2 pr-2 text-right text-gray-200">
                        {d.scan_score != null ? d.scan_score : "—"}
                      </td>
                      <td
                        className={`py-2 pr-2 text-right ${
                          pnlPips > 0
                            ? "text-emerald-400"
                            : pnlPips < 0
                            ? "text-red-400"
                            : "text-gray-400"
                        }`}
                      >
                        {pnlPips != null
                          ? (pnlPips >= 0 ? "+" : "") + Number(pnlPips).toFixed(1)
                          : "—"}
                      </td>
                      <td className="py-2 pr-2 text-center">{govChanged ? "⚠️" : "—"}</td>
                      <td className="py-2 pr-2 text-xs text-gray-500">
                        {truncate(dec.reason, 60)}
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
