import { getAdaptiveLearning } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { fmtTs } from "../../utils/engineFormat";

export default function AdaptiveLearning() {
  const { data, loading, error, unavailable } = useEnginePoll(
    getAdaptiveLearning,
    5000
  );

  const applier = data?.applier || {};
  const scheduler = data?.scheduler || {};
  const tuner = data?.tuner || {};
  const registered = Array.isArray(tuner.registered) ? tuner.registered : [];

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Adaptive Learning"
        subtitle="Governed parameter application, periodic tuning, scan-cycle learning, and registered tuning agents. Analytical timeframe weights do not determine Brain direction."
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
              label="Tuner"
              value={tuner.enabled ? "ENABLED" : "UNAVAILABLE"}
              accent={tuner.enabled ? "text-emerald-400" : "text-gray-500"}
            />
            <StatTile label="Registered Tunables" value={registered.length} />
            <StatTile label="Periodic Runs" value={scheduler.periodic_runs || 0} />
            <StatTile label="Params Applied" value={applier.applied_count || 0} />
          </div>

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
                </tbody>
              </table>
            </Panel>

            <Panel title="Registered tuning agents">
              {registered.length ? (
                <ul className="space-y-1 text-xs text-gray-400">
                  {registered.map((name) => (
                    <li key={name}>{name}</li>
                  ))}
                </ul>
              ) : (
                <div className="py-3 text-center text-xs text-gray-600">
                  No tunables are registered.
                </div>
              )}
            </Panel>
          </div>
        </>
      )}
    </div>
  );
}
