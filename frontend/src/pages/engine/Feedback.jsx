import { getOutcomeFeedback } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import { useEnginePoll } from "../../hooks/useEnginePoll";

// Win rate arrives as a 0..1 fraction here.
function wrColor(wr) {
  const v = Number(wr) || 0;
  if (v >= 0.55) return "text-emerald-400";
  if (v < 0.45) return "text-red-400";
  return "text-yellow-400";
}

function calColor(gap) {
  if (gap === null || gap === undefined) return "text-gray-500";
  if (Math.abs(gap) <= 0.1) return "text-emerald-400";
  if (gap > 0) return "text-red-400"; // overconfident
  return "text-sky-400"; // underconfident
}

function pctFrac(x) {
  return `${Math.round((Number(x) || 0) * 100)}%`;
}

export default function Feedback() {
  const { data, loading, error, unavailable } = useEnginePoll(
    getOutcomeFeedback,
    8000,
  );
  const modules = data?.modules || [];
  const horizons = data?.horizons || [];
  const overallR = Number(data?.overall_avg_r || 0);

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Outcome Feedback"
        subtitle="The loop that closes: each closed trade's realised R links back to the modules that drove it — who's earning their keep, and who's overconfident."
      />

      <EngineNotice
        loading={loading}
        unavailable={unavailable}
        error={error}
        hasData={!!data}
      />

      {data && (
        <>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <StatTile label="Trades Linked" value={data?.total_trades || 0} />
            <StatTile
              label="Overall Win Rate"
              value={pctFrac(data?.overall_win_rate)}
              accent={wrColor(data?.overall_win_rate)}
            />
            <StatTile
              label="Overall Avg R"
              value={`${overallR >= 0 ? "+" : ""}${overallR.toFixed(2)}R`}
              accent={overallR >= 0 ? "text-emerald-400" : "text-red-400"}
            />
          </div>

          <Panel
            title="Per-module accuracy & calibration"
            subtitle="Calibration gap = predicted confidence − realised win rate. ~0 well-calibrated · >0 overconfident · <0 underconfident."
            className="overflow-x-auto"
          >
            <table className="w-full min-w-[560px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="py-2 pr-2">Module</th>
                  <th className="py-2 pr-2 text-right">Trades</th>
                  <th className="py-2 pr-2 text-right">Win Rate</th>
                  <th className="py-2 pr-2 text-right">Avg R</th>
                  <th className="py-2 pr-2 text-right">Avg Conf</th>
                  <th className="py-2 pr-2 text-right">Calibration Gap</th>
                </tr>
              </thead>
              <tbody>
                {modules.length === 0 && (
                  <tr>
                    <td colSpan={6} className="py-12 text-center text-gray-500">
                      {loading
                        ? "Loading…"
                        : "No completed trades linked yet — accuracy appears as trades close."}
                    </td>
                  </tr>
                )}
                {modules.map((m) => (
                  <tr key={m.module} className="border-b border-gray-800">
                    <td className="py-2 pr-2 font-semibold text-gray-100">
                      {m.module}
                    </td>
                    <td className="py-2 pr-2 text-right text-gray-200">
                      {m.trades}
                    </td>
                    <td className={`py-2 pr-2 text-right ${wrColor(m.win_rate)}`}>
                      {Math.round((m.win_rate || 0) * 100)}%
                    </td>
                    <td
                      className={`py-2 pr-2 text-right font-mono ${
                        m.avg_r >= 0 ? "text-emerald-400" : "text-red-400"
                      }`}
                    >
                      {m.avg_r >= 0 ? "+" : ""}
                      {Number(m.avg_r).toFixed(2)}
                    </td>
                    <td className="py-2 pr-2 text-right font-mono text-gray-200">
                      {m.avg_confidence === null
                        ? "—"
                        : Number(m.avg_confidence).toFixed(2)}
                    </td>
                    <td
                      className={`py-2 pr-2 text-right font-mono ${calColor(
                        m.calibration_gap,
                      )}`}
                    >
                      {m.calibration_gap === null
                        ? "—"
                        : `${m.calibration_gap > 0 ? "+" : ""}${Number(
                            m.calibration_gap,
                          ).toFixed(2)}`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>

          <Panel title="Per-horizon outcomes" className="overflow-x-auto">
            <table className="w-full min-w-[420px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="py-2 pr-2">Horizon</th>
                  <th className="py-2 pr-2 text-right">Trades</th>
                  <th className="py-2 pr-2 text-right">Win Rate</th>
                  <th className="py-2 pr-2 text-right">Avg R</th>
                </tr>
              </thead>
              <tbody>
                {horizons.length === 0 && (
                  <tr>
                    <td colSpan={4} className="py-8 text-center text-gray-500">
                      No data yet.
                    </td>
                  </tr>
                )}
                {horizons.map((h) => (
                  <tr key={h.horizon} className="border-b border-gray-800">
                    <td className="py-2 pr-2 font-semibold text-gray-100">
                      {h.horizon}
                    </td>
                    <td className="py-2 pr-2 text-right text-gray-200">
                      {h.trades}
                    </td>
                    <td className={`py-2 pr-2 text-right ${wrColor(h.win_rate)}`}>
                      {Math.round((h.win_rate || 0) * 100)}%
                    </td>
                    <td
                      className={`py-2 pr-2 text-right font-mono ${
                        h.avg_r >= 0 ? "text-emerald-400" : "text-red-400"
                      }`}
                    >
                      {h.avg_r >= 0 ? "+" : ""}
                      {Number(h.avg_r).toFixed(2)}
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
