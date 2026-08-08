import { getReconciliation } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { fmtTs, truncate } from "../../utils/engineFormat";

export default function Reconciliation() {
  const { data, loading, error, unavailable } = useEnginePoll(
    getReconciliation,
    10000,
  );

  const anomalies = data?.anomalies || [];

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Exit Reconciliation"
        subtitle="Broker-reported vs derived exit reasons — discrepancies and anomalies"
      />

      <EngineNotice
        loading={loading}
        unavailable={unavailable}
        error={error}
        hasData={!!data}
      />

      {data && (
        <>
          <div className="grid grid-cols-2 gap-4">
            <StatTile
              label="Discrepancies Found"
              value={anomalies.length}
              accent={anomalies.length > 0 ? "text-yellow-400" : "text-emerald-400"}
            />
            <StatTile
              label="Status"
              value={anomalies.length === 0 ? "✅ Clean" : "⚠ Review Needed"}
              accent={anomalies.length === 0 ? "text-emerald-400" : "text-yellow-400"}
            />
          </div>

          <Panel title="Anomalous Trade Closes" className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="py-2 pr-2">Time</th>
                  <th className="py-2 pr-2">Symbol</th>
                  <th className="py-2 pr-2">Derived Reason</th>
                  <th className="py-2 pr-2">Source</th>
                  <th className="py-2 pr-2">Broker Reason</th>
                  <th className="py-2 pr-2">Broker Comment</th>
                  <th className="py-2 pr-2 text-right">P&amp;L ($)</th>
                  <th className="py-2 pr-2">Correlation</th>
                </tr>
              </thead>
              <tbody>
                {anomalies.length === 0 && (
                  <tr>
                    <td colSpan={8} className="py-12 text-center text-gray-500">
                      {loading
                        ? "Loading…"
                        : "No discrepancies — all exit reasons match."}
                    </td>
                  </tr>
                )}
                {anomalies.map((a, i) => {
                  const mismatch =
                    a.raw_broker_reason &&
                    a.exit_reason &&
                    a.exit_reason !== a.raw_broker_reason;
                  const pnl = a.pnl_dollars;
                  return (
                    <tr key={a.event_id || i} className="border-b border-gray-800">
                      <td className="py-2 pr-2 text-xs text-gray-500">
                        {fmtTs(a.timestamp)}
                      </td>
                      <td className="py-2 pr-2 font-mono font-semibold text-gray-100">
                        {a.symbol || "—"}
                      </td>
                      <td
                        className={`py-2 pr-2 font-mono text-xs ${
                          mismatch ? "text-yellow-400" : "text-gray-300"
                        }`}
                      >
                        {a.exit_reason || "(missing)"}
                      </td>
                      <td className="py-2 pr-2 text-xs text-gray-500">
                        {a.exit_reason_source || "—"}
                      </td>
                      <td
                        className={`py-2 pr-2 font-mono text-xs ${
                          mismatch ? "text-gray-100" : "text-gray-500"
                        }`}
                      >
                        {a.raw_broker_reason || "(none)"}
                      </td>
                      <td className="py-2 pr-2 text-xs text-gray-500">
                        {a.raw_broker_comment || "—"}
                      </td>
                      <td
                        className={`py-2 pr-2 text-right font-mono ${
                          (pnl || 0) >= 0 ? "text-emerald-400" : "text-red-400"
                        }`}
                      >
                        {pnl != null ? `$${Math.abs(pnl).toFixed(2)}` : "—"}
                      </td>
                      <td className="py-2 pr-2 font-mono text-[11px] text-gray-500">
                        {a.correlation_id ? truncate(a.correlation_id, 16) : "—"}
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
