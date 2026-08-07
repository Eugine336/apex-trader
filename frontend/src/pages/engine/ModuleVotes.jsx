import { useMemo, useState } from "react";

import { getModuleVotes } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import SymbolFilter from "../../components/engine/SymbolFilter";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { badgeClass, dirCellClass, dirText } from "../../utils/engineFormat";

export default function ModuleVotes() {
  const { data, loading, error, unavailable } = useEnginePoll(getModuleVotes, 5000);
  const [symbolFilter, setSymbolFilter] = useState("");

  const pairs = data?.pairs || [];
  const modules = data?.modules || [];
  const moduleNames = useMemo(() => modules.map((m) => m.module), [modules]);

  const filtered = useMemo(() => {
    if (!symbolFilter) return pairs;
    const q = symbolFilter.toUpperCase();
    return pairs.filter((p) => (p.pair || "").toUpperCase().includes(q));
  }, [pairs, symbolFilter]);

  const voteByModule = (p) => {
    const m = {};
    (p.votes || []).forEach((v) => {
      m[v.module] = v;
    });
    return m;
  };

  const longLeans = pairs.filter((p) => (p.net_signed || 0) > 0).length;
  const shortLeans = pairs.filter((p) => (p.net_signed || 0) < 0).length;

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Module Votes"
        subtitle="What each brain module saw this scan — the raw evidence before the ranker clusters it"
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
            <StatTile label="Pairs Voting" value={data?.pair_count || 0} />
            <StatTile label="Active Modules" value={modules.length} />
            <StatTile label="LONG Leans" value={longLeans} accent="text-emerald-400" />
            <StatTile label="SHORT Leans" value={shortLeans} accent="text-red-400" />
          </div>

          <Panel title="Module Participation — across all pairs this scan" className="overflow-x-auto">
            {modules.length === 0 ? (
              <div className="py-6 text-center text-xs text-gray-500">
                No module votes yet — they appear here as the bot scans.
              </div>
            ) : (
              <table className="w-full min-w-[520px] text-sm">
                <thead>
                  <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                    <th className="py-2 pr-2">Module</th>
                    <th className="py-2 pr-2">Horizon</th>
                    <th className="py-2 pr-2 text-right">LONG</th>
                    <th className="py-2 pr-2 text-right">SHORT</th>
                    <th className="py-2 pr-2 text-right">NEUTRAL</th>
                    <th className="py-2 pr-2 text-right">Avg Conf</th>
                  </tr>
                </thead>
                <tbody>
                  {modules.map((m) => (
                    <tr key={m.module} className="border-b border-gray-800">
                      <td className="py-2 pr-2 font-medium text-gray-100">{m.module}</td>
                      <td className="py-2 pr-2">
                        <span className={badgeClass("muted")}>{m.horizon}</span>
                      </td>
                      <td className="py-2 pr-2 text-right text-emerald-400">{m.long}</td>
                      <td className="py-2 pr-2 text-right text-red-400">{m.short}</td>
                      <td className="py-2 pr-2 text-right text-gray-500">{m.neutral}</td>
                      <td className="py-2 pr-2 text-right font-mono text-gray-200">
                        {Number(m.avg_confidence || 0).toFixed(2)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>

          <SymbolFilter value={symbolFilter} onChange={setSymbolFilter} />

          <Panel
            title="Vote Grid — green LONG · red SHORT · grey NEUTRAL"
            className="overflow-x-auto"
          >
            <table className="w-full min-w-[640px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="py-2 pr-2">Symbol</th>
                  <th className="py-2 pr-2">Consensus</th>
                  <th className="py-2 pr-2 text-right">Net</th>
                  {moduleNames.map((m) => (
                    <th key={m} className="py-2 text-center text-[10px] normal-case">
                      {m}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {filtered.length === 0 && (
                  <tr>
                    <td
                      colSpan={3 + moduleNames.length}
                      className="py-12 text-center text-gray-500"
                    >
                      No votes recorded yet.
                    </td>
                  </tr>
                )}
                {filtered.map((p) => {
                  const vm = voteByModule(p);
                  const net = p.net_signed || 0;
                  return (
                    <tr key={p.pair} className="border-b border-gray-800">
                      <td className="py-2 pr-2 font-medium text-gray-100">{p.pair}</td>
                      <td className={`py-2 pr-2 ${dirText(p.consensus_direction || p.direction)}`}>
                        {p.consensus_direction || p.direction || "—"}
                      </td>
                      <td
                        className={`py-2 pr-2 text-right font-mono ${dirText(
                          net > 0 ? "LONG" : net < 0 ? "SHORT" : ""
                        )}`}
                      >
                        {Number(net).toFixed(2)}
                      </td>
                      {moduleNames.map((m) => {
                        const v = vm[m];
                        if (!v) {
                          return (
                            <td key={m} className="py-2 text-center text-gray-600">
                              ·
                            </td>
                          );
                        }
                        return (
                          <td
                            key={m}
                            title={`${m}: ${v.direction} conf ${Number(v.confidence).toFixed(
                              2
                            )} (${v.horizon})`}
                            className={`py-1 text-center text-[10px] font-bold ${dirCellClass(
                              v.direction
                            )}`}
                          >
                            {v.direction === "LONG" ? "L" : v.direction === "SHORT" ? "S" : "·"}
                            <div className="text-[9px] font-normal opacity-80">
                              {v.direction !== "NEUTRAL" ? Number(v.confidence).toFixed(2) : ""}
                            </div>
                          </td>
                        );
                      })}
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
