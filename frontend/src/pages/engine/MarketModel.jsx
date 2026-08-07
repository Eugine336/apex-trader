import { useMemo, useState } from "react";

import { getMarketModel } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import SymbolFilter from "../../components/engine/SymbolFilter";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { badgeClass, dirText, fmtTs } from "../../utils/engineFormat";

const TF_ORDER = ["M5", "M15", "H1", "H4", "D1"];

function trendClass(trend) {
  const v = String(trend || "").toUpperCase();
  if (v === "BULLISH") return "text-emerald-400";
  if (v === "BEARISH") return "text-red-400";
  return "text-gray-500";
}

function agreementBadge(agreement) {
  switch (agreement) {
    case "agree":
      return "green";
    case "diverge":
      return "red";
    case "partial":
      return "yellow";
    default:
      return "muted";
  }
}

// Render a single store's (confirmed | developing) bias + per-TF structure.
function StoreColumn({ title, model, accent }) {
  if (!model || Object.keys(model).length === 0) {
    return (
      <div className="flex-1">
        <div className="mb-1 text-xs uppercase tracking-wide text-gray-500">{title}</div>
        <div className="py-3 text-center text-xs text-gray-600">no data</div>
      </div>
    );
  }
  const byTf = {};
  (model.structure || []).forEach((r) => {
    byTf[r.timeframe] = r;
  });
  return (
    <div className="flex-1">
      <div className={`mb-1 text-xs uppercase tracking-wide ${accent}`}>{title}</div>
      <div className="mb-2 flex items-center gap-2 text-sm">
        <span className={`font-semibold ${dirText(model.bias_direction)}`}>
          {model.bias_direction || "—"}
        </span>
        <span className="text-[11px] text-gray-500">
          L {Number(model.long_probability || 0).toFixed(2)} · S{" "}
          {Number(model.short_probability || 0).toFixed(2)} · conf{" "}
          {Number(model.confidence || 0).toFixed(2)}
        </span>
      </div>
      <table className="w-full text-[11px]">
        <tbody>
          {TF_ORDER.filter((tf) => byTf[tf]).map((tf) => {
            const r = byTf[tf];
            return (
              <tr key={tf} className="border-b border-gray-800/60">
                <td className="py-0.5 pr-2 text-gray-500">{tf}</td>
                <td className={`py-0.5 pr-2 font-medium ${trendClass(r.trend)}`}>
                  {r.trend}
                </td>
                <td className="py-0.5 pr-2 text-right font-mono text-gray-400">
                  {Number(r.confidence || 0).toFixed(2)}
                </td>
                <td className="py-0.5 text-right text-[10px] text-gray-500">
                  {r.event && r.event !== "NONE" ? r.event : ""}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function MarketModel() {
  const { data, loading, error, unavailable } = useEnginePoll(getMarketModel, 5000);
  const [symbolFilter, setSymbolFilter] = useState("");

  const symbols = data?.symbols || [];

  const filtered = useMemo(() => {
    if (!symbolFilter) return symbols;
    const q = symbolFilter.toUpperCase();
    return symbols.filter((s) => (s.symbol || "").toUpperCase().includes(q));
  }, [symbols, symbolFilter]);

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Market Model"
        subtitle="Confirmed (closed-candle) vs developing (forming-candle) structure — the dual WorldModel. Divergence is the early warning the management advisory acts on."
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
            <StatTile label="Symbols" value={data?.symbol_count || 0} />
            <StatTile label="Confirmed Models" value={data?.confirmed_count || 0} />
            <StatTile
              label="Developing Models"
              value={data?.developing_count || 0}
              accent={data?.developing_enabled ? "text-sky-400" : "text-gray-500"}
            />
            <StatTile
              label="Diverging"
              value={data?.diverging_count || 0}
              accent="text-red-400"
            />
          </div>

          {!data?.developing_enabled && (
            <Panel title="Developing analysis">
              <div className="py-3 text-center text-xs text-amber-300">
                Developing analysis is not wired — only the confirmed model is shown.
              </div>
            </Panel>
          )}

          <SymbolFilter value={symbolFilter} onChange={setSymbolFilter} />

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            {filtered.length === 0 && (
              <Panel title="Symbols">
                <div className="py-12 text-center text-gray-500">
                  No market models published yet — they appear as the bot analyzes.
                </div>
              </Panel>
            )}
            {filtered.map((s) => (
              <Panel
                key={s.symbol}
                title={
                  <span className="flex items-center justify-between gap-2">
                    <span>{s.symbol}</span>
                    <span className={badgeClass(agreementBadge(s.agreement))}>
                      {s.agreement}
                      {s.diverging_timeframes && s.diverging_timeframes.length > 0
                        ? ` · ${s.diverging_timeframes.join(",")}`
                        : ""}
                    </span>
                  </span>
                }
              >
                <div className="flex gap-4">
                  <StoreColumn
                    title="Confirmed"
                    model={s.confirmed}
                    accent="text-gray-400"
                  />
                  <div className="w-px bg-gray-700/60" />
                  <StoreColumn
                    title="Developing"
                    model={s.developing}
                    accent="text-sky-400"
                  />
                </div>
                <div className="mt-2 text-right text-[10px] text-gray-600">
                  confirmed v{s.confirmed?.version || 0} {fmtTs(s.confirmed?.timestamp)}
                  {s.developing && Object.keys(s.developing).length > 0
                    ? ` · developing v${s.developing.version || 0} ${fmtTs(
                        s.developing.timestamp
                      )}`
                    : ""}
                </div>
              </Panel>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
