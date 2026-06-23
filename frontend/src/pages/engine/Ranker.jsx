import { Fragment, useMemo, useState } from "react";

import { getRanker } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import SymbolFilter from "../../components/engine/SymbolFilter";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { badgeClass, dirText, evText } from "../../utils/engineFormat";

function OppRow({ o, best }) {
  return (
    <div
      className={`mb-1.5 border-l-2 pl-2.5 ${best ? "" : "opacity-80"}`}
      style={{
        borderColor:
          (o.direction || "").toUpperCase() === "LONG"
            ? "#34d399"
            : (o.direction || "").toUpperCase() === "SHORT"
            ? "#f87171"
            : "#6b7280",
      }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className={`text-xs font-bold ${dirText(o.direction)}`}>
          {o.direction} {o.timeframe_class}
        </span>
        {best && <span className={badgeClass("green")}>BEST</span>}
        <span className={`font-mono text-xs font-bold ${evText(o.expected_value)}`}>
          EV {o.expected_value >= 0 ? "+" : ""}
          {Number(o.expected_value).toFixed(2)}R
        </span>
        <span className="ml-auto text-[10px] text-gray-500">
          p_win {Math.round(o.win_prob * 100)}% · rr {Number(o.reward_risk).toFixed(1)} · conf{" "}
          {Number(o.confidence).toFixed(2)} · coh {Math.round(o.coherence * 100)}%
        </span>
      </div>
      <div className="mt-0.5 text-[11px] text-gray-400">
        from: {(o.contributors || []).join(", ") || "none"}
      </div>
    </div>
  );
}

export default function Ranker() {
  const { data, loading, error, unavailable } = useEnginePoll(getRanker, 5000);
  const [symbolFilter, setSymbolFilter] = useState("");
  const [expanded, setExpanded] = useState(null);

  const pairs = data?.pairs || [];
  const horizon = data?.horizon_distribution || {};
  const dirDist = data?.direction_distribution || {};

  const filtered = useMemo(() => {
    if (!symbolFilter) return pairs;
    const q = symbolFilter.toUpperCase();
    return pairs.filter((p) => (p.pair || "").toUpperCase().includes(q));
  }, [pairs, symbolFilter]);

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Opportunity Ranker"
        subtitle="Coherent vote clusters scored as independent trade ideas — best idea on the board wins"
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
              value={data?.execute ? "LIVE" : "SHADOW"}
              accent={data?.execute ? "text-emerald-400" : "text-yellow-400"}
            />
            <StatTile label="Opportunities" value={data?.total_opportunities || 0} />
            <StatTile
              label="Avg EV"
              value={`${(data?.avg_expected_value || 0) >= 0 ? "+" : ""}${Number(
                data?.avg_expected_value || 0
              ).toFixed(2)}R`}
              accent={evText(data?.avg_expected_value || 0)}
            />
            <StatTile
              label="Ranker Overrides"
              value={data?.override_count || 0}
              accent={(data?.override_count || 0) > 0 ? "text-sky-400" : "text-gray-100"}
            />
            <StatTile
              label="SCALP / SWING"
              value={`${horizon.SCALP || 0} / ${horizon.SWING || 0}`}
            />
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <Panel title="Horizon Distribution">
              <div className="flex gap-6">
                <div className="flex-1">
                  <div className="text-xs text-gray-400">SCALP (fast)</div>
                  <div className="font-mono text-2xl font-bold text-gray-100">
                    {horizon.SCALP || 0}
                  </div>
                </div>
                <div className="flex-1">
                  <div className="text-xs text-gray-400">SWING (slow)</div>
                  <div className="font-mono text-2xl font-bold text-gray-100">
                    {horizon.SWING || 0}
                  </div>
                </div>
              </div>
            </Panel>
            <Panel title="Direction Distribution">
              <div className="flex gap-6">
                <div className="flex-1">
                  <div className="text-xs text-emerald-400">LONG</div>
                  <div className="font-mono text-2xl font-bold text-emerald-400">
                    {dirDist.LONG || 0}
                  </div>
                </div>
                <div className="flex-1">
                  <div className="text-xs text-red-400">SHORT</div>
                  <div className="font-mono text-2xl font-bold text-red-400">
                    {dirDist.SHORT || 0}
                  </div>
                </div>
              </div>
            </Panel>
          </div>

          <SymbolFilter value={symbolFilter} onChange={setSymbolFilter} />

          <Panel
            title="Ranked Opportunities — click a row to see every cluster"
            className="overflow-x-auto"
          >
            <table className="w-full min-w-[640px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="py-2 pr-2">Symbol</th>
                  <th className="py-2 pr-2">Best Idea</th>
                  <th className="py-2 pr-2 text-right">EV</th>
                  <th className="py-2 pr-2">Consensus</th>
                  <th className="py-2 pr-2">Selected</th>
                  <th className="py-2 pr-2 text-right">Ideas</th>
                </tr>
              </thead>
              <tbody>
                {filtered.length === 0 && (
                  <tr>
                    <td colSpan={6} className="py-12 text-center text-gray-500">
                      No ranked opportunities yet — they appear here as the bot scans.
                    </td>
                  </tr>
                )}
                {filtered.map((p) => {
                  const isOpen = expanded === p.pair;
                  const b = p.best || {};
                  return (
                    <Fragment key={p.pair}>
                      <tr
                        className="cursor-pointer border-b border-gray-800 hover:bg-gray-700/30"
                        onClick={() => setExpanded(isOpen ? null : p.pair)}
                      >
                        <td className="py-2 pr-2 font-medium text-gray-100">{p.pair}</td>
                        <td className={`py-2 pr-2 font-medium ${dirText(b.direction)}`}>
                          {b.direction} {b.timeframe_class}
                        </td>
                        <td
                          className={`py-2 pr-2 text-right font-mono font-bold ${evText(
                            b.expected_value
                          )}`}
                        >
                          {(b.expected_value || 0) >= 0 ? "+" : ""}
                          {Number(b.expected_value || 0).toFixed(2)}R
                        </td>
                        <td className={`py-2 pr-2 ${dirText(p.consensus_direction)}`}>
                          {p.consensus_direction || "—"}
                        </td>
                        <td className="py-2 pr-2">
                          {p.ranker_override ? (
                            <span className={badgeClass("yellow")}>OVERRIDE</span>
                          ) : p.selected_horizon ? (
                            <span className={badgeClass("green")}>{p.selected_horizon}</span>
                          ) : (
                            <span className={badgeClass("muted")}>—</span>
                          )}
                        </td>
                        <td className="py-2 pr-2 text-right text-gray-200">
                          {p.opportunity_count}
                        </td>
                      </tr>
                      {isOpen && (
                        <tr className="bg-gray-900/40">
                          <td colSpan={6} className="px-3 py-2">
                            {(p.opportunities || []).map((o, i) => (
                              <OppRow key={i} o={o} best={i === 0} />
                            ))}
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
