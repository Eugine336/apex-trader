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

  const queue = data?.queue || {};
  const sizer = data?.quality_sizer || {};
  const displacer = data?.displacer || {};
  const scanner = data?.proactive_scanner || {};
  const watchlist = data?.watchlist || [];

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Cross-Instrument Opportunities"
        subtitle="Portfolio-level opportunism — the global opportunity queue, cross-instrument ranking + quality sizing, position displacement, and the proactive high-EV watchlist. Each layer is opt-in via config.cross_instrument."
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
              label="Queue"
              value={queue.enabled ? "ON" : "OFF"}
              accent={queue.enabled ? "text-emerald-400" : "text-gray-500"}
            />
            <StatTile
              label="Quality Sizing"
              value={sizer.enabled ? "ON" : "OFF"}
              accent={sizer.enabled ? "text-emerald-400" : "text-gray-500"}
            />
            <StatTile
              label="Displacement"
              value={displacer.enabled ? "ON" : "OFF"}
              accent={displacer.enabled ? "text-emerald-400" : "text-gray-500"}
            />
            <StatTile
              label="Proactive Scan"
              value={scanner.enabled ? "ON" : "OFF"}
              accent={scanner.enabled ? "text-emerald-400" : "text-gray-500"}
            />
          </div>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <Panel title="Global opportunity queue">
              <table className="w-full text-[11px]">
                <tbody>
                  <tr className="border-b border-gray-800/60">
                    <td className="py-0.5 text-gray-500">Window</td>
                    <td className="py-0.5 text-right font-mono text-gray-200">
                      {queue.window_ms || 0} ms
                    </td>
                  </tr>
                  <tr className="border-b border-gray-800/60">
                    <td className="py-0.5 text-gray-500">Submitted</td>
                    <td className="py-0.5 text-right font-mono text-gray-200">
                      {queue.submitted || 0}
                    </td>
                  </tr>
                  <tr className="border-b border-gray-800/60">
                    <td className="py-0.5 text-gray-500">Dispatched</td>
                    <td className="py-0.5 text-right font-mono text-gray-200">
                      {queue.dispatched || 0}
                    </td>
                  </tr>
                  <tr className="border-b border-gray-800/60">
                    <td className="py-0.5 text-gray-500">Windows</td>
                    <td className="py-0.5 text-right font-mono text-gray-200">
                      {queue.windows || 0}
                    </td>
                  </tr>
                </tbody>
              </table>
            </Panel>

            <Panel title="Quality sizer · Displacer">
              <table className="w-full text-[11px]">
                <tbody>
                  <tr className="border-b border-gray-800/60">
                    <td className="py-0.5 text-gray-500">Size boost / cut</td>
                    <td className="py-0.5 text-right font-mono text-gray-200">
                      ×{Number(sizer.max_boost || 1).toFixed(2)} /{" "}
                      ×{Number(sizer.min_cut || 1).toFixed(2)}
                    </td>
                  </tr>
                  <tr className="border-b border-gray-800/60">
                    <td className="py-0.5 text-gray-500">Displacements</td>
                    <td className="py-0.5 text-right font-mono text-gray-200">
                      {displacer.total_displacements || 0}
                    </td>
                  </tr>
                  <tr className="border-b border-gray-800/60">
                    <td className="py-0.5 text-gray-500">EV margin</td>
                    <td className="py-0.5 text-right font-mono text-gray-200">
                      {Number(displacer.ev_margin || 0).toFixed(2)}R
                    </td>
                  </tr>
                  <tr className="border-b border-gray-800/60">
                    <td className="py-0.5 text-gray-500">Scanner runs</td>
                    <td className="py-0.5 text-right font-mono text-gray-200">
                      {scanner.scans || 0}
                    </td>
                  </tr>
                </tbody>
              </table>
            </Panel>
          </div>

          <Panel title="Proactive watchlist (approaching high-EV setups)">
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
