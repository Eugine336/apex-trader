import { Fragment, useMemo, useState } from "react";

import { getDecisionTrace } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import SymbolFilter from "../../components/engine/SymbolFilter";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { badgeClass, fmtTs } from "../../utils/engineFormat";

const STAGE_LABELS = {
  ranker: "Ranker",
  correlation: "Correlation",
  margin: "Margin",
  max_trades: "Max Trades",
  entry_engine: "Entry Engine",
  decision_engine: "Decision Engine",
  governor: "Governor",
  planner: "Planner",
  risk_stack: "Risk Stack",
  management: "Management",
};

function stageLabel(s) {
  return STAGE_LABELS[s] || s;
}

function outcomeBadgeVariant(outcome) {
  if (!outcome) return "muted";
  if (outcome === "TRADE_PLACED") return "green";
  if (outcome.startsWith("REJECTED")) return "red";
  if (outcome === "ABORTED") return "yellow";
  return "muted";
}

function verdictColor(verdict) {
  const v = (verdict || "").toUpperCase();
  if (
    v.includes("BLOCK") ||
    v.includes("SKIP") ||
    v.includes("REJECT") ||
    v.includes("VETO") ||
    v.includes("WAIT")
  ) {
    return "#f87171";
  }
  if (
    v.includes("PASS") ||
    v.includes("ENTER") ||
    v.includes("LONG") ||
    v.includes("SHORT") ||
    v.includes("CHANGED")
  ) {
    return "#34d399";
  }
  return "#9ca3af";
}

function verdictTextClass(verdict) {
  const c = verdictColor(verdict);
  if (c === "#f87171") return "text-red-400";
  if (c === "#34d399") return "text-emerald-400";
  return "text-gray-400";
}

function fmtEvidence(ev) {
  if (!ev || typeof ev !== "object") return "";
  return Object.entries(ev)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `${k}=${typeof v === "number" ? v : String(v)}`)
    .join("  ·  ");
}

function StageChain({ trace }) {
  const stages = trace.stages || [];
  const challengesByTarget = useMemo(() => {
    const m = {};
    (trace.challenges || []).forEach((c) => {
      (m[c.target_stage] = m[c.target_stage] || []).push(c);
    });
    return m;
  }, [trace]);

  return (
    <div className="flex flex-col gap-2 px-1 py-2">
      {stages.length === 0 && (
        <div className="text-xs text-gray-500">No stage verdicts recorded.</div>
      )}
      {stages.map((s, i) => (
        <div
          key={i}
          className="border-l-2 pl-2.5"
          style={{ borderColor: verdictColor(s.verdict) }}
        >
          <div className="flex flex-wrap items-center gap-2">
            <span className="min-w-[130px] text-xs font-bold text-gray-100">
              {stageLabel(s.stage)}
            </span>
            <span className={`font-mono text-[11px] font-bold ${verdictTextClass(s.verdict)}`}>
              {s.verdict}
            </span>
            <span className="text-[10px] text-gray-500">by {s.owner}</span>
            <span className="ml-auto text-[10px] text-gray-500">
              conf {Number(s.confidence ?? 0).toFixed(2)}
            </span>
          </div>
          <div className="mt-0.5 text-xs text-gray-400">{s.justification}</div>
          {fmtEvidence(s.evidence) && (
            <div className="mt-0.5 font-mono text-[10px] text-gray-500">
              {fmtEvidence(s.evidence)}
            </div>
          )}
          {(challengesByTarget[s.stage] || []).map((c, j) => (
            <div key={j} className="mt-1 text-[11px] text-yellow-400">
              ⚑ challenged by <strong>{c.challenger}</strong>: {c.reason}
            </div>
          ))}
        </div>
      ))}
      {trace.final_reason && (
        <div className="mt-1 text-[11px] text-gray-500">⇒ {trace.final_reason}</div>
      )}
    </div>
  );
}

function MeterRow({ label, pct, value, barColor }) {
  return (
    <div className="flex items-center gap-2">
      <span className="min-w-[120px] text-xs font-semibold text-gray-400">{label}</span>
      <div className="h-3.5 flex-1 overflow-hidden rounded bg-gray-700">
        <div className="h-full" style={{ width: `${pct}%`, background: barColor }} />
      </div>
      <span className="min-w-[44px] text-right font-mono text-xs font-semibold text-gray-300">
        {value}
      </span>
    </div>
  );
}

export default function DecisionTrace() {
  const fetcher = useMemo(() => () => getDecisionTrace({ limit: 150 }), []);
  const { data, loading, error, unavailable } = useEnginePoll(fetcher, 5000);
  const [symbolFilter, setSymbolFilter] = useState("");
  const [expanded, setExpanded] = useState(null);

  const traces = data?.traces || [];
  const stats = data?.stats || {};
  const funnel = stats.funnel || [];
  const rejections = stats.rejections || [];
  const challenges = stats.challenges || [];
  const confidence = stats.confidence || {};
  const maxFunnel = Math.max(1, ...funnel.map((f) => f.count));

  const filtered = useMemo(() => {
    if (!symbolFilter) return traces;
    const q = symbolFilter.toUpperCase();
    return traces.filter((t) => (t.pair || "").toUpperCase().includes(q));
  }, [traces, symbolFilter]);

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Decision Trace"
        subtitle="Pipeline awareness — every stage's justified verdict, challenges, and where opportunities die"
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
            <StatTile label="Traces" value={stats.total_traces || 0} />
            <StatTile
              label="Trades Placed"
              value={stats.trades_placed || 0}
              accent="text-emerald-400"
            />
            <StatTile
              label="Challenges Raised"
              value={stats.challenge_count || 0}
              accent={(stats.challenge_count || 0) > 0 ? "text-yellow-400" : "text-gray-100"}
            />
            <StatTile label="Rejecting Gates" value={rejections.length} />
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <Panel title="Pipeline Funnel">
              {funnel.every((f) => f.count === 0) ? (
                <div className="py-6 text-center text-xs text-gray-500">
                  No traces recorded yet.
                </div>
              ) : (
                <div className="flex flex-col gap-1.5">
                  {funnel.map((f) => (
                    <MeterRow
                      key={f.stage}
                      label={stageLabel(f.stage)}
                      pct={(f.count / maxFunnel) * 100}
                      value={f.count}
                      barColor="#0ea5e9"
                    />
                  ))}
                </div>
              )}
            </Panel>

            <Panel title="Avg Confidence by Stage">
              {Object.keys(confidence).length === 0 ? (
                <div className="py-6 text-center text-xs text-gray-500">
                  No confidence data yet.
                </div>
              ) : (
                <div className="flex flex-col gap-1.5">
                  {Object.entries(confidence).map(([stage, v]) => (
                    <MeterRow
                      key={stage}
                      label={stageLabel(stage)}
                      pct={Math.max(0, Math.min(100, v * 100))}
                      value={Number(v).toFixed(2)}
                      barColor={v > 0.6 ? "#34d399" : v < 0.35 ? "#f87171" : "#facc15"}
                    />
                  ))}
                </div>
              )}
            </Panel>
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <Panel title="Rejection Breakdown">
              {rejections.length === 0 ? (
                <div className="py-6 text-center text-xs text-gray-500">
                  No rejections recorded yet.
                </div>
              ) : (
                <div className="flex flex-col gap-2.5">
                  {rejections.map((r) => (
                    <div key={r.stage}>
                      <div className="flex items-center gap-2">
                        <span className={`${badgeClass("red")} min-w-[120px] text-center`}>
                          {stageLabel(r.stage)}
                        </span>
                        <span className="font-mono text-xs font-bold text-red-400">
                          {r.count}
                        </span>
                      </div>
                      {(r.reasons || []).slice(0, 3).map((reason, i) => (
                        <div key={i} className="ml-2 mt-0.5 text-[11px] text-gray-500">
                          • {reason}
                        </div>
                      ))}
                    </div>
                  ))}
                </div>
              )}
            </Panel>

            <Panel title="Challenge Feed">
              {challenges.length === 0 ? (
                <div className="py-6 text-center text-xs text-gray-500">
                  No challenges raised — components agree.
                </div>
              ) : (
                <div className="flex max-h-72 flex-col gap-2 overflow-y-auto">
                  {challenges.map((c, i) => (
                    <div key={i} className="border-l-2 border-yellow-500 pl-2.5">
                      <div className="text-xs text-gray-100">
                        <strong>{c.challenger}</strong> ⚑{" "}
                        <strong>{stageLabel(c.target_stage)}</strong>
                        <span className="ml-1.5 text-gray-500">{c.pair}</span>
                      </div>
                      <div className="text-[11px] text-gray-400">{c.reason}</div>
                    </div>
                  ))}
                </div>
              )}
            </Panel>
          </div>

          <SymbolFilter value={symbolFilter} onChange={setSymbolFilter} />

          <Panel
            title="Recent Traces — click to expand the full stage chain"
            className="overflow-x-auto"
          >
            <table className="w-full min-w-[680px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="py-2 pr-2">Time</th>
                  <th className="py-2 pr-2">Symbol</th>
                  <th className="py-2 pr-2">Outcome</th>
                  <th className="py-2 pr-2">Died At</th>
                  <th className="py-2 pr-2 text-right">Stages</th>
                  <th className="py-2 pr-2 text-right">Challenges</th>
                </tr>
              </thead>
              <tbody>
                {filtered.length === 0 && (
                  <tr>
                    <td colSpan={6} className="py-12 text-center text-gray-500">
                      No traces recorded yet — they appear here as the bot scans.
                    </td>
                  </tr>
                )}
                {filtered.map((t, i) => {
                  const key = t.trace_id || i;
                  const isOpen = expanded === key;
                  const challengeCount = (t.challenges || []).length;
                  return (
                    <Fragment key={key}>
                      <tr
                        className="cursor-pointer border-b border-gray-800 hover:bg-gray-700/30"
                        onClick={() => setExpanded(isOpen ? null : key)}
                      >
                        <td className="py-2 pr-2 text-xs text-gray-500">
                          {fmtTs(t._event_ts)}
                        </td>
                        <td className="py-2 pr-2 font-medium text-gray-100">
                          {t.pair || "—"}
                        </td>
                        <td className="py-2 pr-2">
                          <span className={badgeClass(outcomeBadgeVariant(t.final_outcome))}>
                            {t.final_outcome || "—"}
                          </span>
                        </td>
                        <td className="py-2 pr-2 text-xs text-gray-400">
                          {t.rejected_at ? stageLabel(t.rejected_at) : "—"}
                        </td>
                        <td className="py-2 pr-2 text-right text-gray-200">
                          {(t.stages || []).length}
                        </td>
                        <td
                          className={`py-2 pr-2 text-right ${
                            challengeCount ? "text-yellow-400" : "text-gray-400"
                          }`}
                        >
                          {challengeCount}
                        </td>
                      </tr>
                      {isOpen && (
                        <tr className="bg-gray-900/40">
                          <td colSpan={6}>
                            <StageChain trace={t} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
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
