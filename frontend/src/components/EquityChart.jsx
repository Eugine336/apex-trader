import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { formatDateTime, formatMoney } from "../utils/format";

// Renders the cumulative realized P&L curve. `data` is the API equity-curve
// list [{ closed_at, equity }] (oldest → newest).
export default function EquityChart({ data = [] }) {
  if (!data.length) {
    return (
      <div className="flex h-72 items-center justify-center text-sm text-gray-500">
        No closed trades yet — the equity curve will appear once trades settle.
      </div>
    );
  }

  const chartData = data.map((p, i) => ({
    index: i + 1,
    equity: Number(p.equity || 0),
    closed_at: p.closed_at,
  }));

  const last = chartData[chartData.length - 1].equity;
  const lineColor = last >= 0 ? "#34d399" : "#f87171";

  return (
    <div className="h-72 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={chartData} margin={{ top: 10, right: 20, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
          <XAxis
            dataKey="index"
            stroke="#6b7280"
            tick={{ fontSize: 11 }}
            tickLine={false}
          />
          <YAxis
            stroke="#6b7280"
            tick={{ fontSize: 11 }}
            tickLine={false}
            tickFormatter={(v) => formatMoney(v)}
            width={70}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: "#1f2937",
              border: "1px solid #374151",
              borderRadius: "0.5rem",
              color: "#f3f4f6",
            }}
            labelFormatter={(_, payload) =>
              payload?.[0]?.payload?.closed_at
                ? formatDateTime(payload[0].payload.closed_at)
                : ""
            }
            formatter={(value) => [formatMoney(value), "Equity"]}
          />
          <Line
            type="monotone"
            dataKey="equity"
            stroke={lineColor}
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 4 }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
