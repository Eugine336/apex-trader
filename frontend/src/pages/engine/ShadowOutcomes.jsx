import { useMemo, useState } from "react";

import { getShadowOutcomes } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { badgeClass, dirText, truncate } from "../../utils/engineFormat";

function fmtR(r) {
  if (r == null) return "—";
  const v = Number(r);
  return (v >= 0 ? "+" : "") + v.toFixed(2) + "R";
}

function outcomeVariant(outcome) {
  switch ((outcome || "").toUpperCase()) {
    case "WIN":
      return "green";
    case "LOSS":
      return "red";
    case "BE":
      return "yellow";
    case "PARTIAL":
      return "blue";
    default:
      return "muted";
  }
}

function statusVariant(status) {
  if (status === "RESOLVED") return "green";
  if (status === "PENDING") return "blue";
  return "muted";
}

function fmtMs(ms) {
  if (!ms) return "—";
  try {
    const d = new Date(Number(ms));
    return (
      d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" }) +
      " " +
      d.toLocaleTimeString("en-GB", {
        hour12: false,
        hour: "2-digit",
        minute: "2-digit",
      })
    );
  } catch {
    return "—";
  }
}

export default function ShadowOutcomes() {
  const { data, loading, error, unavailable } = useEnginePoll(
    getShadowOutcomes,
    10000,
  );
  const [gateFilter, setGateFilter] = useState("ALL");

  const gates = data?.gates || [];
  const contracts = data?.contracts || [];
  const summary = data?.summary || {};

  const filteredContracts = useMemo(() => {
    if (gateFilter === "ALL") return contracts;
    return contracts.filter((c) => c.rejecting_gate === gateFilter);
  }, [contracts, gateFilter]);

  const gateNames = ["ALL", ...gates.map((g) => g.gate)];
  const total =
    (summary.PENDING || 0) + (summary.RESOLVED || 0) + (summary.EXPIRED || 0);

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Shadow Outcomes"
        subtitle="Rejected/skipped setup counterfactual results — did they win or lose?"
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
            <StatTile label="Pending" value={summary.PENDING || 0} />
            <StatTile
              label="Resolved"
              value={summary.RESOLVED || 0}
              accent="text-emerald-400"
            />
            <StatTile
              label="Expired"
              value={summary.EXPIRED || 0}
              accent="text-gray-400"
            />
            <StatTile label="Total Contracts" value={total} />
          </div>

          {gates.length > 0 && (
            <Panel
              title="Outcomes by Rejecting Gate"
              subtitle="Click a gate to filter the contracts below"
              className="overflow-x-auto"
            >
              <table className="w-full min-w-[560px] text-sm">
                <thead>
                  <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                    <th className="py-2 pr-2">Gate</th>
                    <th className="py-2 pr-2 text-right">Total</th>
                    <th className="py-2 pr-2 text-right">Wins</th>
                    <th className="py-2 pr-2 text-right">Losses</th>
                    <th className="py-2 pr-2 text-right">Expired</th>
                    <th className="py-2 pr-2 text-right">Win Rate</th>
                    <th className="py-2 pr-2 text-right">Avg R</th>
                  </tr>
                </thead>
                <tbody>
                  {gates.map((g) => (
                    <tr
                      key={g.gate}
                      onClick={() => setGateFilter(g.gate)}
                      className="cursor-pointer border-b border-gray-800 hover:bg-gray-700/40"
                    >
                      <td className="py-2 pr-2 font-mono font-semibold text-gray-100">
                        {g.gate}
                      </td>
                      <td className="py-2 pr-2 text-right text-gray-200">
                        {g.total}
                      </td>
                      <td className="py-2 pr-2 text-right text-emerald-400">
                        {g.WIN || 0}
                      </td>
                      <td className="py-2 pr-2 text-right text-red-400">
                        {g.LOSS || 0}
                      </td>
                      <td className="py-2 pr-2 text-right text-gray-500">
                        {g.EXPIRED || 0}
                      </td>
                      <td className="py-2 pr-2 text-right font-semibold text-gray-100">
                        {g.win_rate}%
                      </td>
                      <td className="py-2 pr-2 text-right font-mono text-gray-200">
                        {fmtR(g.avg_r)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>
          )}

          <div className="flex flex-wrap gap-2">
            {gateNames.map((g) => (
              <button
                key={g}
                type="button"
                onClick={() => setGateFilter(g)}
                className={`rounded-md px-3 py-1 text-xs font-medium transition-colors ${
                  gateFilter === g
                    ? "bg-emerald-600/20 text-emerald-400"
                    : "bg-gray-700/60 text-gray-300 hover:bg-gray-700"
                }`}
              >
                {g}
              </button>
            ))}
          </div>

          <Panel title="Recent Shadow Contracts" className="overflow-x-auto">
            <table className="w-full min-w-[920px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="py-2 pr-2">Time</th>
                  <th className="py-2 pr-2">Symbol</th>
                  <th className="py-2 pr-2">Dir</th>
                  <th className="py-2 pr-2 text-right">Entry</th>
                  <th className="py-2 pr-2 text-right">SL</th>
                  <th className="py-2 pr-2 text-right">TP1</th>
                  <th className="py-2 pr-2">Gate</th>
                  <th className="py-2 pr-2">Status</th>
                  <th className="py-2 pr-2">Outcome</th>
                  <th className="py-2 pr-2 text-right">R</th>
                  <th className="py-2 pr-2">Granularity</th>
                  <th className="py-2 pr-2">Correlation</th>
                </tr>
              </thead>
              <tbody>
                {filteredContracts.length === 0 && (
                  <tr>
                    <td colSpan={12} className="py-12 text-center text-gray-500">
                      {loading ? "Loading…" : "No shadow contracts yet."}
                    </td>
                  </tr>
                )}
                {filteredContracts.map((c) => (
                  <tr key={c.contract_id} className="border-b border-gray-800">
                    <td className="py-2 pr-2 text-xs text-gray-500">
                      {fmtMs(c.timestamp)}
                    </td>
                    <td className="py-2 pr-2 font-mono font-semibold text-gray-100">
                      {c.symbol}
                    </td>
                    <td className={`py-2 pr-2 ${dirText(c.direction)}`}>
                      {c.direction}
                    </td>
                    <td className="py-2 pr-2 text-right font-mono text-gray-200">
                      {(c.entry_price || 0).toFixed(5)}
                    </td>
                    <td className="py-2 pr-2 text-right font-mono text-gray-300">
                      {(c.stop_loss || 0).toFixed(5)}
                    </td>
                    <td className="py-2 pr-2 text-right font-mono text-gray-300">
                      {(c.tp1 || 0).toFixed(5)}
                    </td>
                    <td className="py-2 pr-2 font-mono text-xs text-gray-300">
                      {c.rejecting_gate}
                    </td>
                    <td className="py-2 pr-2">
                      <span className={badgeClass(statusVariant(c.status))}>
                        {c.status}
                      </span>
                    </td>
                    <td className="py-2 pr-2">
                      {c.outcome ? (
                        <span className={badgeClass(outcomeVariant(c.outcome))}>
                          {c.outcome}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="py-2 pr-2 text-right font-mono text-gray-200">
                      {fmtR(c.r_multiple)}
                    </td>
                    <td className="py-2 pr-2 text-[11px] text-gray-500">
                      {c.resolution_granularity || "—"}
                    </td>
                    <td className="py-2 pr-2 font-mono text-[11px] text-gray-500">
                      {c.correlation_id ? truncate(c.correlation_id, 16) : "—"}
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
