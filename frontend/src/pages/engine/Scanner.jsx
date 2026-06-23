import { useMemo, useState } from "react";

import { getScanner } from "../../api/engine";
import FactorHeatmap from "../../components/charts/FactorHeatmap";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { badgeClass, dirText } from "../../utils/engineFormat";

const CATS = ["ALL", "FOREX", "COMMODITY", "INDEX", "SYNTHETIC"];
const FACTORS = ["structure", "fvg", "ob", "liquidity", "sweep", "session", "strength"];

function factorColor(v) {
  if (v > 8) return "text-emerald-400";
  if (v > 4) return "text-yellow-400";
  return "text-gray-500";
}

function ScoreBar({ score }) {
  const pct = Math.max(0, Math.min(100, Number(score || 0)));
  const color = pct >= 85 ? "bg-emerald-500" : pct >= 70 ? "bg-yellow-500" : "bg-gray-600";
  return (
    <div className="flex items-center justify-end gap-2">
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-gray-700">
        <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="w-7 text-right font-mono text-xs text-gray-200">{score || 0}</span>
    </div>
  );
}

export default function Scanner() {
  const { data, loading, error, unavailable } = useEnginePoll(getScanner, 5000);
  const [catFilter, setCatFilter] = useState("ALL");

  const sc = data || {};
  const instruments = sc.instruments || [];

  const filtered = useMemo(() => {
    let list = instruments;
    if (catFilter !== "ALL") {
      list = list.filter((i) => (i.category || "").toUpperCase() === catFilter);
    }
    return [...list].sort((a, b) => (b.score || 0) - (a.score || 0));
  }, [instruments, catFilter]);

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Scanner"
        subtitle={`${sc.ready_count || 0} ready · ${sc.watchlist_count || 0} watchlist · ${
          sc.total_count || instruments.length
        } total`}
      />

      <EngineNotice
        loading={loading}
        unavailable={unavailable}
        error={error}
        hasData={!!data}
      />

      {data && (
        <>
          <div className="flex flex-wrap gap-2">
            {CATS.map((c) => (
              <button
                key={c}
                type="button"
                onClick={() => setCatFilter(c)}
                className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                  catFilter === c
                    ? "bg-emerald-600/20 text-emerald-400"
                    : "bg-gray-800 text-gray-400 hover:bg-gray-700/60 hover:text-gray-100"
                }`}
              >
                {c}
              </button>
            ))}
          </div>

          <Panel className="overflow-x-auto p-0">
            <table className="w-full min-w-[760px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="px-4 py-3">Symbol</th>
                  <th className="px-2 py-3">Name</th>
                  <th className="px-2 py-3">Cat</th>
                  <th className="px-2 py-3">Dir</th>
                  <th className="px-2 py-3 text-right">Score</th>
                  <th className="px-2 py-3">Status</th>
                  {FACTORS.map((f) => (
                    <th key={f} className="px-2 py-3 text-right">
                      {f.toUpperCase()}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {filtered.length === 0 && (
                  <tr>
                    <td colSpan={13} className="px-4 py-10 text-center text-gray-500">
                      No instruments match filter
                    </td>
                  </tr>
                )}
                {filtered.map((inst) => {
                  const dir = (inst.direction || "").toUpperCase();
                  const status = (inst.status || "").toUpperCase();
                  const isReady = status === "READY";
                  return (
                    <tr
                      key={inst.symbol}
                      className={`border-b border-gray-800 ${
                        isReady ? "border-l-2 border-l-emerald-500" : ""
                      }`}
                    >
                      <td className="px-4 py-2 font-semibold text-gray-100">
                        {inst.symbol}
                      </td>
                      <td className="px-2 py-2 text-gray-400">{inst.name}</td>
                      <td className="px-2 py-2">
                        <span className={badgeClass("muted")}>
                          {(inst.category || "").toUpperCase()}
                        </span>
                      </td>
                      <td className={`px-2 py-2 font-medium ${dirText(dir)}`}>
                        {dir || "—"}
                      </td>
                      <td className="px-2 py-2 text-right">
                        <ScoreBar score={inst.score || 0} />
                      </td>
                      <td className="px-2 py-2">
                        <span
                          className={badgeClass(
                            status === "READY"
                              ? "green"
                              : status === "WATCHLIST"
                              ? "yellow"
                              : "muted"
                          )}
                        >
                          {status}
                        </span>
                      </td>
                      {FACTORS.map((f) => {
                        const v = inst.factors?.[f] ?? 0;
                        return (
                          <td
                            key={f}
                            className={`px-2 py-2 text-right font-mono ${factorColor(v)}`}
                          >
                            {v}
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Panel>

          <Panel title="Factor Heatmap — Top 20">
            <FactorHeatmap instruments={instruments} />
          </Panel>
        </>
      )}
    </div>
  );
}
