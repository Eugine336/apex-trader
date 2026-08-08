import { useMemo } from "react";

import { getModuleGovernor } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { badgeClass } from "../../utils/engineFormat";

function modeVariant(mode) {
  const m = (mode || "ACTIVE").toUpperCase();
  if (m === "SHADOW") return "yellow";
  if (m === "DISABLED") return "red";
  return "green";
}

function triggerVariant(trigger) {
  const t = (trigger || "accuracy").toLowerCase();
  if (t === "counterfactual") return "purple";
  if (t === "both") return "yellow";
  return "muted";
}

function fmtAge(seconds) {
  const s = Number(seconds || 0);
  if (s <= 0) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

function fmtTime(ts) {
  const t = Number(ts || 0);
  if (!t) return "—";
  try {
    return new Date(t * 1000).toLocaleTimeString();
  } catch {
    return "—";
  }
}

function fmtMarginalR(v) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return `${n >= 0 ? "+" : ""}${n.toFixed(3)}R`;
}

function marginalRColor(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) {
    return "text-gray-500";
  }
  return Number(v) < 0 ? "text-red-400" : "text-emerald-400";
}

const MODE_ORDER = { SHADOW: 0, DISABLED: 1, ACTIVE: 2 };

export default function ModuleGovernor() {
  const { data, loading, error, unavailable } = useEnginePoll(
    getModuleGovernor,
    5000,
  );

  const modules = data?.modules || [];
  const transitions = data?.transitions || [];
  const counts = data?.counts || {};
  const enabled = !!data?.enabled;
  const cfSignal = !!data?.counterfactual_signal;

  const sorted = useMemo(
    () =>
      [...modules].sort((a, b) => {
        const am = MODE_ORDER[(a.mode || "ACTIVE").toUpperCase()] ?? 3;
        const bm = MODE_ORDER[(b.mode || "ACTIVE").toUpperCase()] ?? 3;
        if (am !== bm) return am - bm;
        return (a.module || "").localeCompare(b.module || "");
      }),
    [modules],
  );

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Module Governor"
        subtitle="Shadow mode — a module whose graded accuracy drops keeps running and being measured, but its vote is suppressed (weight 0). It returns to ACTIVE if it recovers, or is DISABLED if it stays poor."
      />

      <EngineNotice
        loading={loading}
        unavailable={unavailable}
        error={error}
        hasData={!!data}
      />

      {data && (
        <>
          {enabled ? (
            <div
              className={`rounded-md border-l-2 px-4 py-3 text-xs text-gray-400 ${
                cfSignal ? "border-purple-500" : "border-gray-600"
              }`}
            >
              Counterfactual signal:{" "}
              <strong className={cfSignal ? "text-purple-400" : "text-gray-400"}>
                {cfSignal ? "ACTIVE" : "OFF"}
              </strong>{" "}
              —{" "}
              {cfSignal
                ? "shadow/disable can be driven by marginal R (better-off-without); reactivation requires both accuracy AND marginal R to recover."
                : "governance uses graded accuracy only."}
            </div>
          ) : (
            <div className="rounded-md border-l-2 border-yellow-500 px-4 py-3 text-sm text-gray-300">
              Module governor is <strong>disabled</strong> — every module votes
              normally. Enable{" "}
              <code className="rounded bg-gray-700 px-1 text-xs">
                module_governor.module_governor_enabled
              </code>{" "}
              to activate shadow mode.
            </div>
          )}

          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <StatTile label="Modules" value={data?.module_count || 0} />
            <StatTile
              label="Active"
              value={counts.ACTIVE || 0}
              accent="text-emerald-400"
            />
            <StatTile
              label="Shadow"
              value={counts.SHADOW || 0}
              accent="text-yellow-400"
            />
            <StatTile
              label="Disabled"
              value={counts.DISABLED || 0}
              accent="text-red-400"
            />
          </div>

          <Panel title="Module Status" className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="py-2 pr-2">Module</th>
                  <th className="py-2 pr-2">Mode</th>
                  <th className="py-2 pr-2 text-right">Acc @ Move</th>
                  <th className="py-2 pr-2 text-right">Marginal R</th>
                  <th className="py-2 pr-2 text-right">In Mode</th>
                  <th className="py-2 pr-2">Reason</th>
                </tr>
              </thead>
              <tbody>
                {sorted.length === 0 && (
                  <tr>
                    <td colSpan={6} className="py-10 text-center text-gray-500">
                      {loading ? "Loading…" : "No governed modules yet."}
                    </td>
                  </tr>
                )}
                {sorted.map((m) => (
                  <tr key={m.module} className="border-b border-gray-800">
                    <td className="py-2 pr-2 font-semibold text-gray-100">
                      {m.module}
                    </td>
                    <td className="py-2 pr-2">
                      <span className={badgeClass(modeVariant(m.mode))}>
                        {(m.mode || "ACTIVE").toUpperCase()}
                      </span>
                    </td>
                    <td className="py-2 pr-2 text-right font-mono text-gray-200">
                      {Number(m.accuracy_at_transition || 0).toFixed(2)}
                    </td>
                    <td
                      className={`py-2 pr-2 text-right font-mono ${marginalRColor(
                        m.marginal_r,
                      )}`}
                      title={
                        m.marginal_r_trades
                          ? `${m.marginal_r_trades} attributed trades${
                              m.better_off_without ? " · better off without" : ""
                            }`
                          : "no attribution data yet"
                      }
                    >
                      {fmtMarginalR(m.marginal_r)}
                    </td>
                    <td className="py-2 pr-2 text-right text-gray-500">
                      {fmtAge(m.seconds_in_mode)}
                    </td>
                    <td className="py-2 pr-2 text-xs text-gray-500">
                      {m.reason || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>

          <Panel title="Transition History" className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="py-2 pr-2">Time</th>
                  <th className="py-2 pr-2">Module</th>
                  <th className="py-2 pr-2">Change</th>
                  <th className="py-2 pr-2">Trigger</th>
                  <th className="py-2 pr-2 text-right">Acc</th>
                  <th className="py-2 pr-2 text-right">Marg R</th>
                  <th className="py-2 pr-2 text-right">N</th>
                  <th className="py-2 pr-2">Reason</th>
                </tr>
              </thead>
              <tbody>
                {transitions.length === 0 && (
                  <tr>
                    <td colSpan={8} className="py-10 text-center text-gray-500">
                      {loading ? "Loading…" : "No transitions recorded yet."}
                    </td>
                  </tr>
                )}
                {transitions.map((t, i) => (
                  <tr
                    key={`${t.module}-${t.timestamp}-${i}`}
                    className="border-b border-gray-800"
                  >
                    <td className="py-2 pr-2 text-xs text-gray-500">
                      {fmtTime(t.timestamp)}
                    </td>
                    <td className="py-2 pr-2 font-semibold text-gray-100">
                      {t.module}
                    </td>
                    <td className="py-2 pr-2 text-xs">
                      <span className={badgeClass(modeVariant(t.old_mode))}>
                        {(t.old_mode || "").toUpperCase()}
                      </span>
                      <span className="mx-1 text-gray-500">→</span>
                      <span className={badgeClass(modeVariant(t.new_mode))}>
                        {(t.new_mode || "").toUpperCase()}
                      </span>
                    </td>
                    <td className="py-2 pr-2">
                      <span className={badgeClass(triggerVariant(t.trigger))}>
                        {(t.trigger || "accuracy").toUpperCase()}
                      </span>
                    </td>
                    <td className="py-2 pr-2 text-right font-mono text-gray-200">
                      {Number(t.accuracy || 0).toFixed(2)}
                    </td>
                    <td
                      className={`py-2 pr-2 text-right font-mono ${marginalRColor(
                        t.marginal_r,
                      )}`}
                    >
                      {t.trigger === "accuracy" || t.marginal_r === undefined
                        ? "—"
                        : fmtMarginalR(t.marginal_r)}
                    </td>
                    <td className="py-2 pr-2 text-right text-gray-500">
                      {t.sample_size || 0}
                    </td>
                    <td className="py-2 pr-2 text-xs text-gray-500">
                      {t.reason || "—"}
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
