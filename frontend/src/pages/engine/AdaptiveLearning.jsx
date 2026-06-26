import { getAdaptiveLearning } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { fmtTs } from "../../utils/engineFormat";

const TF_ORDER = ["D1", "H4", "H1", "M15", "M5", "M1"];

function deltaClass(delta) {
  const v = Number(delta || 0);
  if (v > 0) return "text-emerald-400";
  if (v < 0) return "text-red-400";
  return "text-gray-500";
}

export default function AdaptiveLearning() {
  const { data, loading, error, unavailable } = useEnginePoll(
    getAdaptiveLearning,
    5000
  );

  const weights = data?.weights || {};
  const applier = data?.applier || {};
  const scheduler = data?.scheduler || {};
  const tfs = TF_ORDER.filter(
    (tf) => weights.weights && weights.weights[tf] !== undefined
  );

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Adaptive Learning"
        subtitle="Closed continuous-learning loop — bounded evidence-weight adaptation, per-TF predictive accuracy, the recommendation applier, and the periodic tuning scheduler."
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
              label="Adapting"
              value={weights.adapting ? "YES" : "NO"}
              accent={weights.adapting ? "text-emerald-400" : "text-gray-500"}
            />
            <StatTile
              label="Trades Learned"
              value={`${weights.total_trades || 0} / ${weights.min_trades || 0}`}
            />
            <StatTile label="Recomputes" value={weights.recompute_count || 0} />
            <StatTile label="Params Applied" value={applier.applied_count || 0} />
          </div>

          {!weights.enabled && (
            <Panel title="Weight adaptation">
              <div className="py-3 text-center text-xs text-amber-300">
                Adaptive weight provider not wired — bias uses static defaults.
              </div>
            </Panel>
          )}

          <Panel title="Evidence weights (adapted vs default)">
            {tfs.length === 0 ? (
              <div className="py-3 text-center text-xs text-gray-600">no data</div>
            ) : (
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-gray-500">
                    <th className="py-1 text-left">TF</th>
                    <th className="py-1 text-right">Weight</th>
                    <th className="py-1 text-right">Default</th>
                    <th className="py-1 text-right">Δ last</th>
                    <th className="py-1 text-right">Accuracy</th>
                    <th className="py-1 text-right">Samples</th>
                  </tr>
                </thead>
                <tbody>
                  {tfs.map((tf) => {
                    const acc = weights.accuracy ? weights.accuracy[tf] : null;
                    return (
                      <tr key={tf} className="border-b border-gray-800/60">
                        <td className="py-0.5 pr-2 text-gray-500">{tf}</td>
                        <td className="py-0.5 pr-2 text-right font-mono text-gray-200">
                          {Number(weights.weights[tf] || 0).toFixed(3)}
                        </td>
                        <td className="py-0.5 pr-2 text-right font-mono text-gray-500">
                          {Number(
                            (weights.defaults && weights.defaults[tf]) || 0
                          ).toFixed(3)}
                        </td>
                        <td
                          className={`py-0.5 pr-2 text-right font-mono ${deltaClass(
                            weights.last_change && weights.last_change[tf]
                          )}`}
                        >
                          {weights.last_change && weights.last_change[tf]
                            ? Number(weights.last_change[tf]).toFixed(3)
                            : "—"}
                        </td>
                        <td className="py-0.5 pr-2 text-right font-mono text-gray-400">
                          {acc === null || acc === undefined
                            ? "—"
                            : Number(acc).toFixed(2)}
                        </td>
                        <td className="py-0.5 text-right font-mono text-gray-500">
                          {weights.samples ? weights.samples[tf] || 0 : 0}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </Panel>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <Panel title="Recommendation applier">
              <div className="mb-2 text-[11px] text-gray-500">
                max change {Number((applier.max_change_pct || 0) * 100).toFixed(0)}% ·{" "}
                {applier.enabled ? "enabled" : "disabled"}
              </div>
              {applier.recent && applier.recent.length > 0 ? (
                <table className="w-full text-[11px]">
                  <tbody>
                    {applier.recent
                      .slice()
                      .reverse()
                      .map((r, i) => (
                        <tr key={i} className="border-b border-gray-800/60">
                          <td className="py-0.5 pr-2 text-gray-400">{r.param_name}</td>
                          <td className="py-0.5 pr-2 font-mono text-gray-500">
                            {r.before} → {r.after}
                          </td>
                          <td className="py-0.5 text-right text-[10px] text-gray-600">
                            {fmtTs(r.timestamp ? r.timestamp * 1000 : null)}
                          </td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              ) : (
                <div className="py-3 text-center text-xs text-gray-600">
                  No parameter changes applied yet.
                </div>
              )}
            </Panel>

            <Panel title="Tuning scheduler">
              <table className="w-full text-[11px]">
                <tbody>
                  <tr className="border-b border-gray-800/60">
                    <td className="py-0.5 text-gray-500">Running</td>
                    <td className="py-0.5 text-right font-mono text-gray-200">
                      {scheduler.running ? "yes" : "no"}
                    </td>
                  </tr>
                  <tr className="border-b border-gray-800/60">
                    <td className="py-0.5 text-gray-500">Periodic runs</td>
                    <td className="py-0.5 text-right font-mono text-gray-200">
                      {scheduler.periodic_runs || 0}
                    </td>
                  </tr>
                  <tr className="border-b border-gray-800/60">
                    <td className="py-0.5 text-gray-500">Scan runs</td>
                    <td className="py-0.5 text-right font-mono text-gray-200">
                      {scheduler.scan_runs || 0}
                    </td>
                  </tr>
                  <tr className="border-b border-gray-800/60">
                    <td className="py-0.5 text-gray-500">Weight recomputes</td>
                    <td className="py-0.5 text-right font-mono text-gray-200">
                      {scheduler.weight_recomputes || 0}
                    </td>
                  </tr>
                </tbody>
              </table>
            </Panel>
          </div>
        </>
      )}
    </div>
  );
}
