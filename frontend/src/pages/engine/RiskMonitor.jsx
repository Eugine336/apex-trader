import { getRisk } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { badgeClass } from "../../utils/engineFormat";

const MODE_INFO = {
  NORMAL: { desc: "Full power — standard risk per trade", color: "#34d399" },
  CAUTION: { desc: "Reduced risk per trade", color: "#facc15" },
  RECOVERY: { desc: "Tight risk per trade", color: "#f97316" },
  FROZEN: { desc: "HALTED — no new trades", color: "#f87171" },
};

function GaugeBar({ label, value, max, unit = "", sub, color = "#34d399" }) {
  const v = Number(value || 0);
  const m = Number(max || 0);
  const pct = m > 0 ? Math.max(0, Math.min(100, (v / m) * 100)) : 0;
  return (
    <div className="rounded-lg border border-gray-700 bg-gray-800 p-4 shadow-lg">
      <div className="flex items-baseline justify-between">
        <span className="text-xs font-medium uppercase tracking-wider text-gray-500">
          {label}
        </span>
        <span className="font-mono text-sm font-semibold text-gray-100">
          {v.toFixed(unit === "%" ? 1 : 0)}
          {unit}
          {m > 0 && <span className="text-gray-500"> / {m}{unit}</span>}
        </span>
      </div>
      <div className="mt-2 h-2 overflow-hidden rounded-full bg-gray-700">
        <div className="h-full" style={{ width: `${pct}%`, background: color }} />
      </div>
      {sub && <div className="mt-1.5 text-xs text-gray-500">{sub}</div>}
    </div>
  );
}

function StatRow({ label, value, accent = "text-gray-200" }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-xs text-gray-500">{label}</span>
      <span className={`font-mono text-sm ${accent}`}>{value}</span>
    </div>
  );
}

function eqBadge(v) {
  if (v === "GOOD") return "green";
  if (v === "ACCEPTABLE") return "yellow";
  return "red";
}

function healthBadge(v) {
  if (v === "HEALTHY") return "green";
  if (v === "WARNING") return "yellow";
  return "red";
}

// currency_exposures may arrive as a dict {CUR: pct} or a list of objects.
function exposureEntries(raw) {
  if (!raw) return [];
  if (Array.isArray(raw)) {
    return raw.map((e) => [
      e.currency || e.pair || e.name || "?",
      Number(e.exposure ?? e.pct ?? e.value ?? 0),
    ]);
  }
  return Object.entries(raw).map(([k, v]) => [k, Number(v || 0)]);
}

export default function RiskMonitor() {
  const { data, loading, error, unavailable } = useEnginePoll(getRisk, 5000);

  const r = data || {};
  const mode = r.risk_mode || r.mode || "NORMAL";
  const mi = MODE_INFO[mode] || MODE_INFO.NORMAL;
  const ddPct = r.daily_loss_pct || 0;
  const maxDD = r.max_daily_loss_pct || 3;
  const balance = r.account_balance || 0;
  const remaining = (((maxDD - ddPct) / 100) * balance).toFixed(2);
  const exposures = exposureEntries(r.currency_exposures);
  const warnings = r.warnings || [];
  const spreadAlerts = r.spread_alerts || [];

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Risk Monitor"
        subtitle="Account protection, drawdown mode, and exposure"
      />

      <EngineNotice
        loading={loading}
        unavailable={unavailable}
        error={error}
        hasData={!!data}
      />

      {data && (
        <>
          <Panel>
            <div className="flex flex-col items-center gap-1 py-2">
              <div
                className="rounded-md border-2 px-8 py-3 text-center"
                style={{ borderColor: mi.color, background: `${mi.color}1a` }}
              >
                <div
                  className="font-mono text-3xl font-bold"
                  style={{ color: mi.color }}
                >
                  {mode}
                </div>
                <div className="mt-1 text-xs text-gray-400">{mi.desc}</div>
                <div
                  className="mt-2 font-mono text-sm font-semibold"
                  style={{ color: mi.color }}
                >
                  {(r.current_risk_pct || 0).toFixed(1)}% risk per trade
                </div>
              </div>
            </div>
          </Panel>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <GaugeBar
              label="Daily Drawdown"
              value={ddPct}
              max={maxDD}
              unit="%"
              color="#f87171"
              sub={`$${remaining} remaining before freeze`}
            />
            <GaugeBar
              label="Open Positions"
              value={r.open_trade_count || 0}
              max={r.max_open_trades || 6}
              color="#34d399"
              sub={`Max ${r.max_open_trades || 6} positions`}
            />
            <GaugeBar
              label="Account Exposure"
              value={r.exposure_pct || r.total_exposure_pct || 0}
              max={100}
              unit="%"
              color="#38bdf8"
              sub={`Balance: $${balance.toLocaleString("en-US", {
                minimumFractionDigits: 2,
              })}`}
            />
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <Panel title="Execution Quality">
              <div className="grid gap-3">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-gray-500">Quality</span>
                  <span className={badgeClass(eqBadge(r.execution_quality))}>
                    {r.execution_quality || "N/A"}
                  </span>
                </div>
                <StatRow
                  label="Avg Slippage"
                  value={`${(r.avg_slippage_pips || 0).toFixed(1)} pips`}
                />
                <StatRow
                  label="Avg Latency"
                  value={`${(r.avg_latency_ms || 0).toFixed(0)} ms`}
                />
                <div className="flex items-center justify-between">
                  <span className="text-xs text-gray-500">Spread</span>
                  <span className={badgeClass(r.spread_is_wide ? "red" : "green")}>
                    {r.spread_is_wide ? "WIDE" : "NORMAL"}
                  </span>
                </div>
                <StatRow label="Requotes" value={r.requote_count || 0} />
              </div>
            </Panel>

            <Panel title="Risk Stats">
              <div className="grid gap-3">
                <StatRow
                  label="Win Rate Today"
                  value={`${(r.win_rate_today || 0).toFixed(1)}%`}
                  accent="text-emerald-400"
                />
                <StatRow
                  label="Profit Factor"
                  value={(r.profit_factor || 0).toFixed(2)}
                />
                <StatRow
                  label="Max DD Today"
                  value={`${(r.max_drawdown_today || 0).toFixed(1)}%`}
                  accent="text-red-400"
                />
                <StatRow
                  label="Consecutive Wins"
                  value={r.consecutive_wins || 0}
                  accent="text-emerald-400"
                />
                <StatRow
                  label="Consecutive Losses"
                  value={r.consecutive_losses || 0}
                  accent="text-red-400"
                />
                <StatRow label="Score Threshold" value={r.score_threshold || 0} />
                <div className="flex items-center justify-between">
                  <span className="text-xs text-gray-500">Health</span>
                  <span className={badgeClass(healthBadge(r.health))}>
                    {r.health || "N/A"}
                  </span>
                </div>
                <StatRow
                  label="Daily P&L %"
                  value={`${(r.daily_pnl_pct || 0).toFixed(2)}%`}
                  accent={(r.daily_pnl_pct || 0) >= 0 ? "text-emerald-400" : "text-red-400"}
                />
                <StatRow
                  label="Weekly P&L %"
                  value={`${(r.weekly_pnl_pct || 0).toFixed(2)}%`}
                  accent={(r.weekly_pnl_pct || 0) >= 0 ? "text-emerald-400" : "text-red-400"}
                />
              </div>
            </Panel>
          </div>

          <Panel title="Currency Exposure">
            {exposures.length === 0 ? (
              <div className="py-6 text-center text-xs text-gray-500">
                No currency exposure.
              </div>
            ) : (
              <div className="flex flex-col gap-2">
                {exposures.map(([cur, pct]) => {
                  const width = Math.max(0, Math.min(100, Math.abs(pct)));
                  return (
                    <div key={cur} className="flex items-center gap-2">
                      <span className="min-w-[56px] font-mono text-xs font-semibold text-gray-300">
                        {cur}
                      </span>
                      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-700">
                        <div
                          className="h-full bg-sky-500"
                          style={{ width: `${width}%` }}
                        />
                      </div>
                      <span className="min-w-[56px] text-right font-mono text-xs text-gray-300">
                        {pct.toFixed(1)}%
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </Panel>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <Panel title="⚠ Warnings">
              {warnings.length === 0 ? (
                <div className="py-4 text-center text-xs text-gray-500">
                  No active warnings
                </div>
              ) : (
                <div className="flex flex-col gap-2">
                  {warnings.map((w, i) => (
                    <div
                      key={i}
                      className="rounded-md border border-yellow-500/30 bg-yellow-500/10 px-3 py-2 text-xs text-yellow-300"
                    >
                      {w}
                    </div>
                  ))}
                </div>
              )}
            </Panel>
            <Panel title="📡 Spread Alerts">
              {spreadAlerts.length === 0 ? (
                <div className="py-4 text-center text-xs text-gray-500">
                  No spread alerts
                </div>
              ) : (
                <div className="flex flex-col gap-2">
                  {spreadAlerts.map((a, i) => (
                    <div
                      key={i}
                      className="rounded-md border border-orange-500/30 bg-orange-500/10 px-3 py-2 text-xs text-orange-300"
                    >
                      {a}
                    </div>
                  ))}
                </div>
              )}
            </Panel>
          </div>
        </>
      )}
    </div>
  );
}
