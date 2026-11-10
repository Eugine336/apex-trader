import { getCrossInstrument } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import { useEnginePoll } from "../../hooks/useEnginePoll";

export default function CrossInstrument() {
  const { data, loading, error, unavailable } = useEnginePoll(
    getCrossInstrument,
    5000
  );

  const scanner = data?.proactive_scanner || {};
  const watchlist = data?.watchlist || [];

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Cross-Instrument Opportunities"
        subtitle="The proactive scanner is observational and cannot create or authorize orders."
      />

      <EngineNotice
        loading={loading}
        unavailable={unavailable}
        error={error}
        hasData={!!data}
      />

      {data && (
        <>
          <Panel title="Proactive scanner">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <div className="text-[10px] text-gray-500">Scans</div>
                  <div className="font-mono text-sm text-gray-200">
                    {scanner.scans || 0}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-gray-500">Watchlist entries</div>
                  <div className="font-mono text-sm text-gray-200">
                    {watchlist.length}
                  </div>
                </div>
              </div>
              <p className="mt-3 text-[11px] text-gray-500">
                Watchlist observations do not authorize or submit trades.
              </p>
          </Panel>

          <Panel title="Observational watchlist">
            {watchlist.length === 0 ? (
              <div className="py-3 text-center text-xs text-gray-600">
                No instruments on the watchlist.
              </div>
            ) : (
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-gray-500">
                    <th className="py-1 text-left">Symbol</th>
                    <th className="py-1 text-left">Direction</th>
                    <th className="py-1 text-right">EV (R)</th>
                  </tr>
                </thead>
                <tbody>
                  {watchlist.map((w, i) => (
                    <tr key={i} className="border-b border-gray-800/60">
                      <td className="py-0.5 pr-2 text-gray-300">{w.symbol}</td>
                      <td className="py-0.5 pr-2 text-gray-400">{w.direction}</td>
                      <td className="py-0.5 text-right font-mono text-emerald-400">
                        {Number(w.ev || 0).toFixed(2)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>
        </>
      )}
    </div>
  );
}
