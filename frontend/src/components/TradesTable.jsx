import {
  directionBadgeClass,
  formatDateTime,
  formatMoney,
  pnlColor,
} from "../utils/format";

// Reusable closed-trade history table. `trades` is a list[TradeResponse].
export default function TradesTable({ trades = [], emptyMessage = "No trades yet." }) {
  if (!trades.length) {
    return (
      <div className="rounded-lg border border-gray-700 bg-gray-800 p-8 text-center text-sm text-gray-500">
        {emptyMessage}
      </div>
    );
  }

  return (
    <div className="apex-scroll overflow-x-auto rounded-lg border border-gray-700">
      <table className="min-w-full divide-y divide-gray-700 text-sm">
        <thead className="bg-gray-800">
          <tr className="text-left text-xs uppercase tracking-wider text-gray-400">
            <th className="px-4 py-3">Symbol</th>
            <th className="px-4 py-3">Direction</th>
            <th className="px-4 py-3 text-right">Entry</th>
            <th className="px-4 py-3 text-right">Exit</th>
            <th className="px-4 py-3 text-right">P&amp;L</th>
            <th className="px-4 py-3 text-right">Pips</th>
            <th className="px-4 py-3">Exit Reason</th>
            <th className="px-4 py-3">Closed</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-700">
          {trades.map((t, i) => (
            <tr
              key={t.id ?? `${t.ticket}-${i}`}
              className={i % 2 === 0 ? "bg-gray-800" : "bg-gray-750"}
            >
              <td className="px-4 py-3 font-medium text-gray-100">{t.symbol}</td>
              <td className="px-4 py-3">
                <span
                  className={`inline-block rounded px-2 py-0.5 text-xs font-semibold ${directionBadgeClass(
                    t.direction
                  )}`}
                >
                  {String(t.direction || "").toUpperCase()}
                </span>
              </td>
              <td className="px-4 py-3 text-right tabular-nums text-gray-300">
                {Number(t.entry_price || 0)}
              </td>
              <td className="px-4 py-3 text-right tabular-nums text-gray-300">
                {Number(t.exit_price || 0)}
              </td>
              <td className={`px-4 py-3 text-right tabular-nums font-semibold ${pnlColor(t.pnl)}`}>
                {formatMoney(t.pnl)}
              </td>
              <td className={`px-4 py-3 text-right tabular-nums ${pnlColor(t.pnl_pips)}`}>
                {Number(t.pnl_pips || 0).toFixed(1)}
              </td>
              <td className="px-4 py-3 text-gray-400">{t.exit_reason || "—"}</td>
              <td className="px-4 py-3 text-gray-400">{formatDateTime(t.closed_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
