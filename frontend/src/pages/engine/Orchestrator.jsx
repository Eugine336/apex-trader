import { Fragment, useMemo, useState } from "react";

import { getOrchestrator } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import SymbolFilter from "../../components/engine/SymbolFilter";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { badgeClass, dirText, sizeText } from "../../utils/engineFormat";

function sizeHex(m) {
  const n = Number(m || 0);
  if (n >= 0.9) return "#34d399";
  if (n >= 0.7) return "#facc15";
  if (n <= 0) return "#f87171";
  return "#0ea5e9";
}

function DimBar({ d }) {
  const m = d.multiplier ?? d.avg_multiplier ?? 0;
  const pct = Math.round(Number(m) * 100);
  return (
    <div className="mb-1.5">
      <div className="flex justify-between text-[11px]">
        <span className="text-gray-400">{d.name}</span>
        <span className={`font-mono ${sizeText(m)}`}>×{Number(m).toFixed(2)}</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded bg-gray-700">
        <div className="h-full" style={{ width: `${pct}%`, background: sizeHex(m) }} />
      </div>
      {d.reason && <div className="mt-0.5 text-[10px] text-gray-500">{d.reason}</div>}
    </div>
  );
}

export default function Orchestrator() {
  const fetcher = useMemo(() => () => getOrchestrator({ limit: 100 }), []);
  const { data, loading, error, unavailable } = useEnginePoll(fetcher, 5000);
  const [expanded, setExpanded] = useState(null);
  const [symbolFilter, setSymbolFilter] = useState("");

  const proposals = data?.proposals || [];
  const stats = data?.stats || {};
  const cfg = data?.config || {};

  const filtered = useMemo(() => {
    if (!symbolFilter) return proposals;
    const q = symbolFilter.toUpperCase();
    return proposals.filter((p) => (p.pair || "").toUpperCase().includes(q));
  }, [proposals, symbolFilter]);

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Orchestrator"
        subtitle="The round table — every stage's evidence folded into one bounded graded size. Weak dimensions dim the trade; only physics vetoes."
      />

      <EngineNotice
        loading={loading}
        unavailable={unavailable}
        error={error}
        hasData={!!data}
      />

      {data && (
        <>
          <div className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-5">
            <StatTile
              label="Mode"
              value={cfg.enabled ? (cfg.apply_sizing ? "LIVE SIZING" : "RECORD ONLY") : "OFF"}
              accent={cfg.apply_sizing ? "text-emerald-400" : "text-yellow-400"}
            />
            <StatTile label="Proposals" value={stats.total || 0} />
            <StatTile
              label="Avg Size ×"
              value={Number(stats.avg_size_multiplier || 0).toFixed(2)}
              accent={sizeText(stats.avg_size_multiplier || 0)}
            />
            <StatTile
              label="Physics Vetoes"
              value={stats.vetoed || 0}
              accent={(stats.vetoed || 0) > 0 ? "text-red-400" : "text-gray-100"}
            />
            <StatTile label="Size Floor" value={`×${Number(cfg.size_floor || 0).toFixed(2)}`} />
          </div>

          <Panel title="Dimensions dragging size down most (avg multiplier)">
            {(stats.dimensions || []).length === 0 ? (
              <div className="py-4 text-center text-xs text-gray-500">No dimensions yet.</div>
            ) : (
              (stats.dimensions || []).map((d) => <DimBar key={d.name} d={d} />)
            )}
          </Panel>

          <SymbolFilter value={symbolFilter} onChange={setSymbolFilter} />

          <Panel
            title="Recent proposals — click a row to see every dimension"
            className="overflow-x-auto"
          >
            <table className="w-full min-w-[640px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="py-2 pr-2">Symbol</th>
                  <th className="py-2 pr-2">Direction</th>
                  <th className="py-2 pr-2">Horizon</th>
                  <th className="py-2 pr-2 text-right">Size ×</th>
                  <th className="py-2 pr-2">Applied</th>
                  <th className="py-2 pr-2">Verdict</th>
                </tr>
              </thead>
              <tbody>
                {filtered.length === 0 && (
                  <tr>
                    <td colSpan={6} className="py-12 text-center text-gray-500">
                      No orchestrator proposals yet — they appear here as entries are graded.
                    </td>
                  </tr>
                )}
                {filtered.map((p, idx) => {
                  const key = `${p.pair}-${idx}`;
                  const isOpen = expanded === key;
                  return (
                    <Fragment key={key}>
                      <tr
                        className="cursor-pointer border-b border-gray-800 hover:bg-gray-700/30"
                        onClick={() => setExpanded(isOpen ? null : key)}
                      >
                        <td className="py-2 pr-2 font-medium text-gray-100">{p.pair}</td>
                        <td className={`py-2 pr-2 ${dirText(p.direction)}`}>{p.direction}</td>
                        <td className="py-2 pr-2 text-gray-300">{p.horizon || "—"}</td>
                        <td
                          className={`py-2 pr-2 text-right font-mono font-bold ${sizeText(
                            p.size_multiplier
                          )}`}
                        >
                          ×{Number(p.size_multiplier || 0).toFixed(2)}
                        </td>
                        <td className="py-2 pr-2">
                          {p.applied ? (
                            <span className={badgeClass("green")}>YES</span>
                          ) : (
                            <span className={badgeClass("muted")}>no</span>
                          )}
                        </td>
                        <td
                          className={`py-2 pr-2 ${
                            p.vetoed ? "text-red-400" : "text-gray-400"
                          }`}
                        >
                          {p.vetoed ? `VETO: ${p.veto_reason}` : "graded"}
                        </td>
                      </tr>
                      {isOpen && (
                        <tr className="bg-gray-900/40">
                          <td colSpan={6} className="px-3 py-2">
                            {(p.dimensions || []).map((d, i) => (
                              <DimBar key={i} d={d} />
                            ))}
                            {(p.dimensions || []).length === 0 && (
                              <div className="text-xs text-gray-500">
                                Physics veto — no dimensions graded.
                              </div>
                            )}
                          </td>
                        </tr>
                      )}
                    </Fragment>
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
