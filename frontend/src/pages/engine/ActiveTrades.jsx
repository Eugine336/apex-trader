import { getActiveTrades } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { badgeClass, dirText } from "../../utils/engineFormat";

function stageBadge(stage) {
  if (stage === "TRAILING") return "green";
  if (stage === "TP1_HIT" || stage === "BREAKEVEN") return "blue";
  return "yellow";
}

export default function ActiveTrades() {
  const { data, loading, error, unavailable } = useEnginePoll(
    getActiveTrades,
    5000
  );

  const trades = data?.trades || [];
  const totalPnl = trades.reduce((s, t) => s + (t.pnl_dollars || 0), 0);
  const avgScore = trades.length
    ? (trades.reduce((s, t) => s + (t.score || 0), 0) / trades.length).toFixed(0)
    : 0;
  const totalLots = trades.reduce((s, t) => s + (t.lot_size || 0), 0);

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Active Trades"
        subtitle={`${trades.length} open position${trades.length !== 1 ? "s" : ""}`}
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
            <StatTile label="Open Positions" value={trades.length} />
            <StatTile
              label="Total P&L"
              value={`${totalPnl >= 0 ? "+" : "-"}$${Math.abs(totalPnl).toFixed(2)}`}
              accent={totalPnl >= 0 ? "text-emerald-400" : "text-red-400"}
            />
            <StatTile label="Total Lots" value={totalLots.toFixed(2)} />
            <StatTile label="Avg Score" value={avgScore} />
          </div>

          <Panel className="overflow-x-auto p-0">
            <table className="w-full min-w-[920px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="px-4 py-3">#</th>
                  <th className="px-2 py-3">Instrument</th>
                  <th className="px-2 py-3">Direction</th>
                  <th className="px-2 py-3 text-right">Entry</th>
                  <th className="px-2 py-3 text-right">Current</th>
                  <th className="px-2 py-3 text-right">SL</th>
                  <th className="px-2 py-3 text-right">TP1</th>
                  <th className="px-2 py-3 text-right">TP2</th>
                  <th className="px-2 py-3 text-right">Pips</th>
                  <th className="px-2 py-3 text-right">P&L ($)</th>
                  <th className="px-2 py-3 text-right">Lots</th>
                  <th className="px-2 py-3 text-right">Score</th>
                  <th className="px-2 py-3">Stage</th>
                  <th className="px-2 py-3">Setup</th>
                </tr>
              </thead>
              <tbody>
                {trades.length === 0 && (
                  <tr>
                    <td colSpan={14} className="px-4 py-10 text-center text-gray-500">
                      No active trades — the sniper is watching.
                    </td>
                  </tr>
                )}
                {trades.map((t, i) => {
                  const pnl = t.pnl_dollars || 0;
                  const dir = (t.direction || "").toUpperCase();
                  return (
                    <tr key={t.id} className="border-b border-gray-800">
                      <td className="px-4 py-2 text-gray-500">{i + 1}</td>
                      <td className="px-2 py-2 font-semibold text-gray-100">
                        {t.instrument}
                      </td>
                      <td className={`px-2 py-2 font-medium ${dirText(dir)}`}>
                        {dir}
                      </td>
                      <td className="px-2 py-2 text-right font-mono text-xs">
                        {(t.entry_price || 0).toFixed(5)}
                      </td>
                      <td className="px-2 py-2 text-right font-mono text-xs">
                        {(t.current_price || 0).toFixed(5)}
                      </td>
                      <td className="px-2 py-2 text-right font-mono text-xs">
                        {(t.stop_loss || 0).toFixed(5)}
                      </td>
                      <td className="px-2 py-2 text-right font-mono text-xs">
                        {(t.tp1 || 0).toFixed(5)}
                      </td>
                      <td className="px-2 py-2 text-right font-mono text-xs">
                        {(t.tp2 || 0).toFixed(5)}
                      </td>
                      <td
                        className={`px-2 py-2 text-right font-mono text-xs ${
                          (t.pnl_pips || 0) >= 0 ? "text-emerald-400" : "text-red-400"
                        }`}
                      >
                        {(t.pnl_pips || 0).toFixed(1)}
                      </td>
                      <td
                        className={`px-2 py-2 text-right font-mono text-xs ${
                          pnl >= 0 ? "text-emerald-400" : "text-red-400"
                        }`}
                      >
                        {pnl >= 0 ? "+" : "-"}${Math.abs(pnl).toFixed(2)}
                      </td>
                      <td className="px-2 py-2 text-right font-mono text-xs">
                        {(t.lot_size || 0).toFixed(2)}
                      </td>
                      <td className="px-2 py-2 text-right font-mono text-xs">
                        {t.score || 0}
                      </td>
                      <td className="px-2 py-2">
                        <span className={badgeClass(stageBadge(t.stage))}>
                          {t.stage}
                        </span>
                      </td>
                      <td className="px-2 py-2">
                        {t.timeframe_class ? (
                          <span
                            className={badgeClass("blue")}
                            title={`candidate ${t.candidate_id || "—"}${
                              (t.contributing_modules || []).length
                                ? " · from: " + t.contributing_modules.join(", ")
                                : ""
                            }`}
                          >
                            {t.timeframe_class}
                          </span>
                        ) : (
                          <span className="text-xs text-gray-600">—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
              {trades.length > 0 && (
                <tfoot>
                  <tr className="border-t border-gray-700 text-xs font-semibold text-gray-300">
                    <td colSpan={9} className="px-4 py-2 text-right">
                      TOTALS
                    </td>
                    <td
                      className={`px-2 py-2 text-right font-mono ${
                        totalPnl >= 0 ? "text-emerald-400" : "text-red-400"
                      }`}
                    >
                      {totalPnl >= 0 ? "+" : "-"}${Math.abs(totalPnl).toFixed(2)}
                    </td>
                    <td className="px-2 py-2 text-right font-mono">
                      {totalLots.toFixed(2)}
                    </td>
                    <td className="px-2 py-2 text-right font-mono">{avgScore}</td>
                    <td />
                    <td />
                  </tr>
                </tfoot>
              )}
            </table>
          </Panel>
        </>
      )}
    </div>
  );
}
