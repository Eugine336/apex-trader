import { getOperations } from "../../api/engine";
import EquityChart from "../../components/EquityChart";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { badgeClass, dirText } from "../../utils/engineFormat";

function pct(x, dp = 0) {
  return `${((Number(x) || 0) * 100).toFixed(dp)}%`;
}

function tsAgo(ts) {
  if (!ts) return "—";
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

// Drawdown / limit ratio → traffic-light text colour.
function limitColor(value, limit) {
  const v = Math.abs(Number(value) || 0);
  const l = Math.abs(Number(limit) || 0);
  if (l <= 0) return "text-gray-100";
  const ratio = v / l;
  if (ratio > 0.8) return "text-red-400";
  if (ratio > 0.5) return "text-yellow-400";
  return "text-emerald-400";
}

function layerColor(state) {
  if (state === "active") return "text-emerald-400";
  if (state === "error") return "text-red-400";
  if (state === "disabled") return "text-amber-400";
  return "text-gray-500";
}

function HealthBar({ health, watchdog }) {
  const h = health || {};
  const wd = watchdog || {};
  const status = h.status || "ok";
  const broker = h.broker || {};
  const statusOk = status === "ok";
  return (
    <Panel>
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-100">System Health</h2>
        <span className={badgeClass(statusOk ? "green" : "muted")}>
          {String(status).toUpperCase()}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-6">
        <StatTile
          label="Running"
          value={h.running ? "YES" : "no"}
          accent={h.running ? "text-emerald-400" : "text-red-400"}
        />
        <StatTile
          label="Broker"
          value={broker.any_connected ? "CONNECTED" : "OFFLINE"}
          accent={broker.any_connected ? "text-emerald-400" : "text-red-400"}
        />
        <StatTile label="Open Positions" value={h.open_positions ?? 0} />
        <StatTile
          label="Last Tick"
          value={h.last_tick_age_seconds != null ? `${h.last_tick_age_seconds}s` : "—"}
          accent={wd.stalled ? "text-red-400" : "text-gray-100"}
        />
        <StatTile
          label="Memory"
          value={h.memory_mb != null ? `${h.memory_mb} MB` : "—"}
        />
        <StatTile
          label="Heartbeats"
          value={wd.beats ?? 0}
          accent={wd.stalled ? "text-red-400" : "text-emerald-400"}
        />
      </div>
      {(h.warnings || []).length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {h.warnings.map((w, i) => (
            <span
              key={i}
              className="rounded bg-red-500/10 px-2 py-0.5 text-xs text-red-400"
            >
              ⚠ {w}
            </span>
          ))}
        </div>
      )}
    </Panel>
  );
}

function DrawdownPanel({ drawdown, equityCurve }) {
  const dd = drawdown || {};
  const state = dd.state || "NORMAL";
  const stateOk = state === "NORMAL" || state === "";
  const curve = (equityCurve || []).map((p) => ({
    equity: p.equity,
    closed_at: p.ts ? new Date(Number(p.ts) * 1000).toISOString() : undefined,
  }));
  return (
    <Panel>
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-100">Drawdown &amp; Equity</h2>
        <span className={badgeClass(stateOk ? "green" : "red")}>
          {state || "NORMAL"}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-6">
        <StatTile
          label="Daily Drawdown"
          value={`${dd.current_daily_pct ?? 0}%`}
          accent={limitColor(dd.current_daily_pct, dd.daily_limit)}
        />
        <StatTile
          label="Rolling Drawdown"
          value={`${dd.current_rolling_pct ?? 0}%`}
          accent={limitColor(dd.current_rolling_pct, dd.rolling_limit)}
        />
        <StatTile label="Hard Stop" value={`${dd.hard_stop_limit ?? 0}%`} />
        <StatTile
          label="Sizing Factor"
          value={dd.sizing_factor != null ? dd.sizing_factor : 1}
          accent={Number(dd.sizing_factor) < 1 ? "text-yellow-400" : "text-gray-100"}
        />
        <StatTile label="Peak Equity" value={dd.peak_equity ?? 0} />
        <StatTile
          label="Flatten?"
          value={dd.should_flatten ? "YES" : "no"}
          accent={dd.should_flatten ? "text-red-400" : "text-gray-100"}
        />
      </div>
      <div className="mt-4">
        {curve.length > 0 ? (
          <EquityChart data={curve} />
        ) : (
          <span className="text-sm text-gray-500">
            No equity history yet — fills as trades close.
          </span>
        )}
      </div>
    </Panel>
  );
}

function OpenPositions({ positions, regimeByPair }) {
  const rows = positions || [];
  return (
    <Panel className="overflow-x-auto">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-100">Open Positions</h2>
        <span className={badgeClass("muted")}>{rows.length}</span>
      </div>
      <table className="w-full min-w-[760px] text-sm">
        <thead>
          <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
            <th className="py-2 pr-2">Instrument</th>
            <th className="py-2 pr-2">Dir</th>
            <th className="py-2 pr-2 text-right">Entry</th>
            <th className="py-2 pr-2 text-right">Current</th>
            <th className="py-2 pr-2 text-right">P&L (pips)</th>
            <th className="py-2 pr-2 text-right">P&L ($)</th>
            <th className="py-2 pr-2 text-right">SL</th>
            <th className="py-2 pr-2 text-right">TP1</th>
            <th className="py-2 pr-2">Regime</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={9} className="py-8 text-center text-gray-500">
                No open positions.
              </td>
            </tr>
          )}
          {rows.map((t) => (
            <tr key={t.id} className="border-b border-gray-800">
              <td className="py-2 pr-2 font-semibold text-gray-100">
                {t.instrument}
              </td>
              <td className={`py-2 pr-2 ${dirText(t.direction)}`}>{t.direction}</td>
              <td className="py-2 pr-2 text-right font-mono text-xs">
                {t.entry_price}
              </td>
              <td className="py-2 pr-2 text-right font-mono text-xs">
                {t.current_price}
              </td>
              <td
                className={`py-2 pr-2 text-right font-mono text-xs ${
                  (t.pnl_pips || 0) >= 0 ? "text-emerald-400" : "text-red-400"
                }`}
              >
                {t.pnl_pips}
              </td>
              <td
                className={`py-2 pr-2 text-right font-mono text-xs ${
                  (t.pnl_dollars || 0) >= 0 ? "text-emerald-400" : "text-red-400"
                }`}
              >
                ${t.pnl_dollars}
              </td>
              <td className="py-2 pr-2 text-right font-mono text-xs">{t.stop_loss}</td>
              <td className="py-2 pr-2 text-right font-mono text-xs">{t.tp1}</td>
              <td className="py-2 pr-2 text-gray-400">
                {(regimeByPair && regimeByPair[t.instrument]) || "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}

function Exposure({ exposure }) {
  const e = exposure || {};
  const perPair = e.per_pair || {};
  const perRegime = e.per_regime || {};
  return (
    <Panel title="Exposure Breakdown">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatTile
          label="Positions"
          value={`${e.total_positions ?? 0}${
            e.max_positions ? ` / ${e.max_positions}` : ""
          }`}
        />
        <StatTile label="Long" value={e.long_positions ?? 0} accent="text-emerald-400" />
        <StatTile label="Short" value={e.short_positions ?? 0} accent="text-red-400" />
        <StatTile label="Directional Skew" value={`${e.directional_pct ?? 0}%`} />
      </div>
      <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2">
        <div>
          <div className="mb-1.5 text-xs font-medium uppercase tracking-wider text-gray-500">
            By Pair
          </div>
          {Object.keys(perPair).length === 0 ? (
            <span className="text-sm text-gray-500">—</span>
          ) : (
            <div className="flex flex-wrap gap-2">
              {Object.entries(perPair).map(([k, v]) => (
                <span key={k} className={badgeClass("muted")}>
                  {k}: {v}
                </span>
              ))}
            </div>
          )}
        </div>
        <div>
          <div className="mb-1.5 text-xs font-medium uppercase tracking-wider text-gray-500">
            By Regime
          </div>
          {Object.keys(perRegime).length === 0 ? (
            <span className="text-sm text-gray-500">—</span>
          ) : (
            <div className="flex flex-wrap gap-2">
              {Object.entries(perRegime).map(([k, v]) => (
                <span key={k} className={badgeClass("muted")}>
                  {k}: {v}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </Panel>
  );
}

function SimpleTable({ title, columns, rows, empty, render }) {
  return (
    <Panel className="overflow-x-auto">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-100">{title}</h2>
        <span className={badgeClass("muted")}>{rows.length}</span>
      </div>
      <table className="w-full min-w-[640px] text-sm">
        <thead>
          <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
            {columns.map((c) => (
              <th
                key={c.key}
                className={`py-2 pr-2 ${c.right ? "text-right" : ""}`}
              >
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td
                colSpan={columns.length}
                className="py-8 text-center text-gray-500"
              >
                {empty}
              </td>
            </tr>
          )}
          {rows.map(render)}
        </tbody>
      </table>
    </Panel>
  );
}

function LayerPulse({ layers }) {
  const entries = Object.entries(layers || {});
  return (
    <Panel>
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-100">Adaptive Layer Pulse</h2>
        <span className={badgeClass("muted")}>{entries.length}</span>
      </div>
      {entries.length === 0 ? (
        <span className="text-sm text-gray-500">No adaptive layers reported.</span>
      ) : (
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          {entries.map(([name, info]) => (
            <div
              key={name}
              className="rounded-lg border border-gray-700 bg-gray-900/40 p-3"
            >
              <div className="text-xs text-gray-500">{name}</div>
              <div className={`mt-1 text-base font-semibold ${layerColor(info.state)}`}>
                {String(info.state).toUpperCase()}
              </div>
              {info.reason && info.state !== "active" && (
                <div className="text-xs text-gray-500">{info.reason}</div>
              )}
              {info.store_mb > 0 && (
                <div className="text-xs text-gray-500">{info.store_mb} MB</div>
              )}
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

function Performance({ perf }) {
  const p = perf || {};
  const tick = p.tick || {};
  const comps = p.components || [];
  const recs = p.recommendations || [];
  const maxAvg = comps.reduce((m, c) => Math.max(m, Number(c.avg_ms) || 0), 0) || 1;
  return (
    <Panel>
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-100">Tick Latency Profile</h2>
        <span className={badgeClass(p.enabled ? "green" : "muted")}>
          {p.enabled ? "LIVE" : "OFF"}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-6">
        <StatTile label="Tick Avg" value={`${(tick.avg_ms || 0).toFixed(1)} ms`} />
        <StatTile label="Tick p50" value={`${(tick.p50_ms || 0).toFixed(1)} ms`} />
        <StatTile label="Tick p95" value={`${(tick.p95_ms || 0).toFixed(1)} ms`} />
        <StatTile label="Tick Max" value={`${(tick.max_ms || 0).toFixed(1)} ms`} />
        <StatTile label="Ticks" value={tick.tick_count ?? 0} />
        <StatTile
          label="Slow Ticks"
          value={tick.slow_tick_count ?? 0}
          accent={(tick.slow_tick_count || 0) > 0 ? "text-yellow-400" : "text-emerald-400"}
        />
      </div>

      <div className="mt-4 overflow-x-auto">
        <table className="w-full min-w-[640px] text-sm">
          <thead>
            <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
              <th className="py-2 pr-2">Component</th>
              <th className="py-2 pr-2 text-right">Avg</th>
              <th className="py-2 pr-2 text-right">p50</th>
              <th className="py-2 pr-2 text-right">p95</th>
              <th className="py-2 pr-2 text-right">Max</th>
              <th className="py-2 pr-2 text-right">Calls</th>
              <th className="py-2 pr-2">Share</th>
            </tr>
          </thead>
          <tbody>
            {comps.length === 0 && (
              <tr>
                <td colSpan={7} className="py-8 text-center text-gray-500">
                  No component timings yet — profile fills as ticks run.
                </td>
              </tr>
            )}
            {comps.map((c) => (
              <tr key={c.component} className="border-b border-gray-800">
                <td className="py-2 pr-2 font-semibold text-gray-100">
                  {c.component}
                </td>
                <td className="py-2 pr-2 text-right font-mono text-xs">
                  {(c.avg_ms || 0).toFixed(2)} ms
                </td>
                <td className="py-2 pr-2 text-right font-mono text-xs">
                  {(c.p50_ms || 0).toFixed(2)}
                </td>
                <td className="py-2 pr-2 text-right font-mono text-xs">
                  {(c.p95_ms || 0).toFixed(2)}
                </td>
                <td className="py-2 pr-2 text-right font-mono text-xs">
                  {(c.max_ms || 0).toFixed(2)}
                </td>
                <td className="py-2 pr-2 text-right font-mono text-xs">{c.calls}</td>
                <td className="py-2 pr-2">
                  <div className="h-2 w-full overflow-hidden rounded-full bg-gray-700">
                    <div
                      className="h-full bg-emerald-500"
                      style={{
                        width: `${Math.min(
                          ((Number(c.avg_ms) || 0) / maxAvg) * 100,
                          100
                        )}%`,
                      }}
                    />
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {recs.length > 0 && (
        <div className="mt-4 flex flex-col gap-2">
          {recs.map((r, i) => (
            <div
              key={i}
              className={`rounded-md px-3 py-2 text-xs ${
                r.severity === "high"
                  ? "border border-red-500/30 bg-red-500/10 text-red-400"
                  : "border border-yellow-500/30 bg-yellow-500/10 text-yellow-300"
              }`}
            >
              {r.suggestion}
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

export default function Operations() {
  const { data, loading, error, unavailable } = useEnginePoll(getOperations, 5000);

  const regimeByPair = {};
  (data?.regime_map || []).forEach((r) => {
    regimeByPair[r.pair] = r.regime;
  });

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Operations"
        subtitle="Control room — health, drawdown, exposure, regime, governor actions, and tick latency"
      />

      <EngineNotice
        loading={loading}
        unavailable={unavailable}
        error={error}
        hasData={!!data}
      />

      {data && (
        <>
          <HealthBar health={data.health} watchdog={data.watchdog} />
          <DrawdownPanel drawdown={data.drawdown} equityCurve={data.equity_curve} />
          <OpenPositions
            positions={data.open_positions}
            regimeByPair={regimeByPair}
          />
          <Exposure exposure={data.exposure} />

          <SimpleTable
            title="Risk Events — Blocked Trades"
            columns={[
              { key: "pair", label: "Pair" },
              { key: "rule", label: "Rule" },
              { key: "reason", label: "Reason" },
              { key: "when", label: "When", right: true },
            ]}
            rows={(data.risk_events || []).slice(0, 30)}
            empty="No risk blocks recorded."
            render={(e, i) => (
              <tr key={i} className="border-b border-gray-800">
                <td className="py-2 pr-2 text-gray-100">{e.pair}</td>
                <td className="py-2 pr-2 text-gray-400">{e.rule}</td>
                <td className="py-2 pr-2 text-gray-400">{e.reason}</td>
                <td className="py-2 pr-2 text-right text-xs text-gray-500">
                  {tsAgo(e.ts)}
                </td>
              </tr>
            )}
          />

          <SimpleTable
            title="Regime Map (per pair)"
            columns={[
              { key: "pair", label: "Pair" },
              { key: "regime", label: "Regime" },
              { key: "confidence", label: "Confidence", right: true },
              { key: "duration", label: "Duration", right: true },
            ]}
            rows={(data.regime_map || []).slice(0, 40)}
            empty="No regimes classified yet."
            render={(p) => (
              <tr key={p.pair} className="border-b border-gray-800">
                <td className="py-2 pr-2 font-semibold text-gray-100">{p.pair}</td>
                <td className="py-2 pr-2 text-gray-400">{p.regime}</td>
                <td className="py-2 pr-2 text-right font-mono text-xs">
                  {pct(p.confidence)}
                </td>
                <td className="py-2 pr-2 text-right font-mono text-xs">
                  {p.duration_sec ? `${Math.round(p.duration_sec)}s` : "—"}
                </td>
              </tr>
            )}
          />

          <LayerPulse layers={data.layer_status} />

          <SimpleTable
            title="Module Governor Actions"
            columns={[
              { key: "module", label: "Module" },
              { key: "transition", label: "Transition" },
              { key: "reason", label: "Reason" },
              { key: "when", label: "When", right: true },
            ]}
            rows={data.governor_actions || []}
            empty="No governor actions yet."
            render={(a, i) => (
              <tr key={i} className="border-b border-gray-800">
                <td className="py-2 pr-2 text-gray-100">{a.module}</td>
                <td className="py-2 pr-2 text-gray-400">
                  {a.from_state} → {a.to_state}
                </td>
                <td className="py-2 pr-2 text-gray-400">{a.reason}</td>
                <td className="py-2 pr-2 text-right text-xs text-gray-500">
                  {tsAgo(a.ts)}
                </td>
              </tr>
            )}
          />

          <Performance perf={data.performance} />
        </>
      )}
    </div>
  );
}
