import { getGovernor } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import { useEnginePoll } from "../../hooks/useEnginePoll";
import { badgeClass, dirText, fmtTs } from "../../utils/engineFormat";

const BLOCK_BADGES = {
  daily_loss_cap: "red",
  max_positions: "orange",
  currency_exposure: "yellow",
  sector_exposure: "yellow",
  correlated_positions: "purple",
};

function blockBadge(key) {
  return BLOCK_BADGES[(key || "").toLowerCase()] || "muted";
}

function ExposureTable({ title, data, max }) {
  const entries = Object.entries(data || {});
  return (
    <Panel title={title}>
      {entries.length === 0 ? (
        <div className="py-6 text-center text-xs text-gray-500">
          No open exposure.
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {entries.map(([key, count]) => {
            const pct = max > 0 ? Math.min(100, (count / max) * 100) : 0;
            const atLimit = count >= max;
            return (
              <div key={key} className="flex items-center gap-2">
                <span className="min-w-[70px] font-mono text-xs font-semibold text-gray-300">
                  {key}
                </span>
                <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-700">
                  <div
                    className={`h-full ${atLimit ? "bg-red-500" : "bg-emerald-500"}`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
                <span
                  className={`min-w-[50px] text-right font-mono text-xs font-semibold ${
                    atLimit ? "text-red-400" : "text-gray-300"
                  }`}
                >
                  {count}/{max}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

export default function Governor() {
  const { data, loading, error, unavailable } = useEnginePoll(getGovernor, 5000);

  const g = data || {};
  const halted = !!g.trading_halted;
  const dailyPct = Number(g.daily_pnl_pct || 0);
  const cap = Number(g.daily_loss_cap_pct || 3);
  const lossFill = Math.max(0, Math.min(100, (-dailyPct / cap) * 100));
  const blocks = g.recent_blocks || [];

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Portfolio Governor"
        subtitle="Position caps, currency & sector concentration, daily loss cap"
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
            <div className="rounded-lg border border-gray-700 bg-gray-800 p-4 shadow-lg">
              <div className="text-xs font-medium uppercase tracking-wider text-gray-500">
                Trading Status
              </div>
              <div className="mt-2">
                <span className={badgeClass(halted ? "red" : "green")}>
                  {halted ? "HALTED" : g.enabled ? "ACTIVE" : "OFF"}
                </span>
              </div>
            </div>
            <StatTile
              label="Daily P&L"
              value={`${dailyPct >= 0 ? "+" : ""}${dailyPct.toFixed(2)}%`}
              accent={
                dailyPct > 0
                  ? "text-emerald-400"
                  : dailyPct < 0
                  ? "text-red-400"
                  : "text-gray-100"
              }
            />
            <StatTile
              label="Open Positions"
              value={`${g.open_positions || 0}/${g.max_open_positions || 0}`}
            />
            <StatTile
              label="Recent Blocks"
              value={blocks.length}
              accent={blocks.length > 0 ? "text-yellow-400" : "text-gray-100"}
            />
          </div>

          <Panel
            title="Daily Loss Cap"
            subtitle={`cap −${cap.toFixed(1)}% · resume above −${Number(
              g.daily_loss_recovery_pct || 0
            ).toFixed(1)}%`}
          >
            <div className="h-2 overflow-hidden rounded-full bg-gray-700">
              <div
                className={`h-full ${
                  halted
                    ? "bg-red-500"
                    : lossFill > 66
                    ? "bg-yellow-500"
                    : "bg-emerald-500"
                }`}
                style={{ width: `${lossFill}%` }}
              />
            </div>
            <div className="mt-2 text-xs text-gray-400">
              {halted
                ? `Trading halted — daily loss breached the −${cap.toFixed(
                    1
                  )}% cap. Entries resume once the day recovers above −${Number(
                    g.daily_loss_recovery_pct || 0
                  ).toFixed(1)}%.`
                : `Daily P&L at ${dailyPct >= 0 ? "+" : ""}${dailyPct.toFixed(
                    2
                  )}% of balance.`}
            </div>
          </Panel>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <ExposureTable
              title="Currency Exposure"
              data={g.currency_exposure}
              max={g.max_currency_exposure || 0}
            />
            <ExposureTable
              title="Sector Exposure"
              data={g.sector_exposure}
              max={g.max_sector_exposure || 0}
            />
          </div>

          <Panel title="Recent Governor Blocks" className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <thead>
                <tr className="border-b border-gray-700 text-left text-xs uppercase text-gray-500">
                  <th className="py-2 pr-2">Time</th>
                  <th className="py-2 pr-2">Symbol</th>
                  <th className="py-2 pr-2">Dir</th>
                  <th className="py-2 pr-2">Blocked By</th>
                  <th className="py-2 pr-2">Reason</th>
                </tr>
              </thead>
              <tbody>
                {blocks.length === 0 && (
                  <tr>
                    <td colSpan={5} className="py-12 text-center text-gray-500">
                      {loading
                        ? "Loading…"
                        : "No governor blocks recorded — entries are within all portfolio limits."}
                    </td>
                  </tr>
                )}
                {blocks.map((b, i) => {
                  const dir = b.direction || "";
                  return (
                    <tr key={i} className="border-b border-gray-800">
                      <td className="py-2 pr-2 text-xs text-gray-500">
                        {fmtTs(b.timestamp)}
                      </td>
                      <td className="py-2 pr-2 font-medium text-gray-100">
                        {b.symbol || "—"}
                      </td>
                      <td className={`py-2 pr-2 ${dirText(dir)}`}>{dir || "—"}</td>
                      <td className="py-2 pr-2">
                        <span className={badgeClass(blockBadge(b.blocked_by))}>
                          {b.blocked_by || "—"}
                        </span>
                      </td>
                      <td className="py-2 pr-2 text-xs text-gray-500">
                        {b.reason || "—"}
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
