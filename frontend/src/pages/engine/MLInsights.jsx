import { getMLInsights } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { badgeClass } from "../../utils/engineFormat";

// Win rate fields here arrive as 0..100 percentages.
function wrColor(wr) {
  const v = Number(wr) || 0;
  if (v >= 60) return "text-emerald-400";
  if (v >= 50) return "text-yellow-400";
  return "text-red-400";
}

function rating(wr) {
  if (wr >= 70) return { text: "STRONG", variant: "green" };
  if (wr >= 55) return { text: "MODERATE", variant: "yellow" };
  return { text: "WEAK", variant: "red" };
}

// Horizontal diverging bars for the ML score weight adjustments.
function WeightBars({ data }) {
  const entries = data ? Object.entries(data) : [];
  if (entries.length === 0) {
    return (
      <div className="py-6 text-center text-xs text-gray-500">
        No weight adjustments yet — the optimizer is still at its priors.
      </div>
    );
  }
  const maxAbs = Math.max(1, ...entries.map(([, v]) => Math.abs(Number(v) || 0)));
  return (
    <div className="flex flex-col gap-2">
      {entries.map(([name, value]) => {
        const v = Number(value) || 0;
        const pct = (Math.abs(v) / maxAbs) * 50; // half-width per side
        const positive = v >= 0;
        return (
          <div key={name} className="flex items-center gap-2">
            <span className="min-w-[120px] text-xs text-gray-400">
              {name.replace(/_weight$/, "").replace(/_/g, " ")}
            </span>
            <div className="relative h-2 flex-1 overflow-hidden rounded-full bg-gray-700">
              <div className="absolute left-1/2 top-0 h-full w-px bg-gray-500" />
              <div
                className={`absolute top-0 h-full ${
                  positive ? "bg-emerald-500" : "bg-red-500"
                }`}
                style={{
                  width: `${pct}%`,
                  left: positive ? "50%" : `${50 - pct}%`,
                }}
              />
            </div>
            <span
              className={`min-w-[44px] text-right font-mono text-xs ${
                positive ? "text-emerald-400" : "text-red-400"
              }`}
            >
              {v >= 0 ? "+" : ""}
              {v.toFixed(1)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export default function MLInsights() {
  const { data, loading, error, unavailable } = useEnginePoll(getMLInsights, 0);

  const regimes = data?.regime_stats ? Object.entries(data.regime_stats) : [];
  const sessions = data?.session_stats ? Object.entries(data.session_stats) : [];
  const pairs = data?.pair_stats
    ? Object.entries(data.pair_stats).sort(
        (a, b) => (b[1].win_rate || 0) - (a[1].win_rate || 0),
      )
    : [];

  return (
    <div className="space-y-6">
      <EngineHeader
        title="ML Insights"
        subtitle="Adaptive learner adjustments and analytics"
      />

      <EngineNotice
        loading={loading}
        unavailable={unavailable}
        error={error}
        hasData={!!data}
      />

      {data && (
        <>
          <Panel
            title="Score Weight Adjustments"
            subtitle="Positive = ML increased factor importance"
          >
            <WeightBars data={data.score_adjustments} />
          </Panel>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Panel title="Regime Performance" className="overflow-x-auto">
              <table className="w-full min-w-[360px] text-sm">
                <thead>
                  <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                    <th className="py-2 pr-2">Regime</th>
                    <th className="py-2 pr-2 text-right">Trades</th>
                    <th className="py-2 pr-2 text-right">Win Rate</th>
                    <th className="py-2 pr-2">Recommendation</th>
                  </tr>
                </thead>
                <tbody>
                  {regimes.length === 0 && (
                    <tr>
                      <td colSpan={4} className="py-6 text-center text-gray-500">
                        No regime data
                      </td>
                    </tr>
                  )}
                  {regimes.map(([name, s]) => (
                    <tr key={name} className="border-b border-gray-800">
                      <td className="py-2 pr-2 font-semibold text-gray-100">
                        {name}
                      </td>
                      <td className="py-2 pr-2 text-right text-gray-200">
                        {s.trades || 0}
                      </td>
                      <td className={`py-2 pr-2 text-right ${wrColor(s.win_rate)}`}>
                        {(s.win_rate || 0).toFixed(1)}%
                      </td>
                      <td className="py-2 pr-2">
                        <span
                          className={badgeClass(
                            (s.recommendation || "").toLowerCase() === "trade"
                              ? "green"
                              : "red",
                          )}
                        >
                          {(s.recommendation || "N/A").toUpperCase()}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>

            <Panel title="Session Performance" className="overflow-x-auto">
              <table className="w-full min-w-[360px] text-sm">
                <thead>
                  <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                    <th className="py-2 pr-2">Session</th>
                    <th className="py-2 pr-2 text-right">Trades</th>
                    <th className="py-2 pr-2 text-right">Win Rate</th>
                    <th className="py-2 pr-2">Aggression</th>
                  </tr>
                </thead>
                <tbody>
                  {sessions.length === 0 && (
                    <tr>
                      <td colSpan={4} className="py-6 text-center text-gray-500">
                        No session data
                      </td>
                    </tr>
                  )}
                  {sessions.map(([name, s]) => {
                    const agg = (s.aggression || "").toLowerCase();
                    return (
                      <tr key={name} className="border-b border-gray-800">
                        <td className="py-2 pr-2 font-semibold text-gray-100">
                          {name}
                        </td>
                        <td className="py-2 pr-2 text-right text-gray-200">
                          {s.trades || 0}
                        </td>
                        <td
                          className={`py-2 pr-2 text-right ${wrColor(s.win_rate)}`}
                        >
                          {(s.win_rate || 0).toFixed(1)}%
                        </td>
                        <td className="py-2 pr-2">
                          <span
                            className={badgeClass(
                              agg === "high"
                                ? "yellow"
                                : agg === "low"
                                  ? "muted"
                                  : "green",
                            )}
                          >
                            {(s.aggression || "N/A").toUpperCase()}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </Panel>
          </div>

          <Panel title="Pair Performance" className="overflow-x-auto">
            <table className="w-full min-w-[520px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="py-2 pr-2">Pair</th>
                  <th className="py-2 pr-2 text-right">Trades</th>
                  <th className="py-2 pr-2 text-right">Win Rate</th>
                  <th className="py-2 pr-2 text-right">Size Multiplier</th>
                  <th className="py-2 pr-2">Rating</th>
                </tr>
              </thead>
              <tbody>
                {pairs.length === 0 && (
                  <tr>
                    <td colSpan={5} className="py-6 text-center text-gray-500">
                      No pair data
                    </td>
                  </tr>
                )}
                {pairs.map(([name, s]) => {
                  const r = rating(s.win_rate || 0);
                  const mult = s.size_mult || 1;
                  return (
                    <tr key={name} className="border-b border-gray-800">
                      <td className="py-2 pr-2 font-semibold text-gray-100">
                        {name}
                      </td>
                      <td className="py-2 pr-2 text-right text-gray-200">
                        {s.trades || 0}
                      </td>
                      <td className={`py-2 pr-2 text-right ${wrColor(s.win_rate)}`}>
                        {(s.win_rate || 0).toFixed(1)}%
                      </td>
                      <td
                        className={`py-2 pr-2 text-right font-mono ${
                          mult >= 1 ? "text-emerald-400" : "text-red-400"
                        }`}
                      >
                        {mult.toFixed(1)}x
                      </td>
                      <td className="py-2 pr-2">
                        <span className={badgeClass(r.variant)}>{r.text}</span>
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
