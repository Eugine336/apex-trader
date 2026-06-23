import { directionBadgeClass, formatDateTime } from "../utils/format";

// Open-positions table. `positions` is a list[PositionResponse].
export default function PositionsTable({ positions = [], emptyMessage = "No open positions." }) {
  if (!positions.length) {
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
            <th className="px-4 py-3 text-right">Lots</th>
            <th className="px-4 py-3 text-right">Entry</th>
            <th className="px-4 py-3 text-right">SL</th>
            <th className="px-4 py-3 text-right">TP1</th>
            <th className="px-4 py-3 text-right">TP2</th>
            <th className="px-4 py-3">Opened</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-700">
          {positions.map((p, i) => (
            <tr
              key={p.order_id ?? i}
              className={i % 2 === 0 ? "bg-gray-800" : "bg-gray-750"}
            >
              <td className="px-4 py-3 font-medium text-gray-100">{p.symbol}</td>
              <td className="px-4 py-3">
                <span
                  className={`inline-block rounded px-2 py-0.5 text-xs font-semibold ${directionBadgeClass(
                    p.direction
                  )}`}
                >
                  {String(p.direction || "").toUpperCase()}
                </span>
              </td>
              <td className="px-4 py-3 text-right tabular-nums text-gray-300">
                {Number(p.lots || 0)}
              </td>
              <td className="px-4 py-3 text-right tabular-nums text-gray-300">
                {Number(p.entry_price || 0)}
              </td>
              <td className="px-4 py-3 text-right tabular-nums text-gray-400">
                {Number(p.sl || 0)}
              </td>
              <td className="px-4 py-3 text-right tabular-nums text-gray-400">
                {Number(p.tp1 || 0)}
              </td>
              <td className="px-4 py-3 text-right tabular-nums text-gray-400">
                {Number(p.tp2 || 0)}
              </td>
              <td className="px-4 py-3 text-gray-400">{formatDateTime(p.open_time)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
