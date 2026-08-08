import {
  Bar,
  BarChart,
  Cell,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { formatMoney } from "../utils/format";

// Daily realized P&L bar chart. `data` is the API daily-pnl list
// [{ date, pnl, trades }] (oldest → newest). Bars are green for positive
// days and red for negative.
export default function DailyPnLChart({ data = [] }) {
  if (!data.length) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-gray-500">
        No daily P&amp;L yet — bars appear once trades settle.
      </div>
    );
  }

  const chartData = data.map((p) => ({
    date: p.date,
    pnl: Number(p.pnl || 0),
    trades: Number(p.trades || 0),
  }));

  return (
    <div className="h-64 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={chartData} margin={{ top: 10, right: 20, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
          <XAxis
            dataKey="date"
            stroke="#6b7280"
            tick={{ fontSize: 11 }}
            tickLine={false}
            tickFormatter={(d) => (d ? String(d).slice(5) : "")}
          />
          <YAxis
            stroke="#6b7280"
            tick={{ fontSize: 11 }}
            tickLine={false}
            tickFormatter={(v) => formatMoney(v)}
            width={70}
          />
          <Tooltip
            cursor={{ fill: "#37415133" }}
            contentStyle={{
              backgroundColor: "#1f2937",
              border: "1px solid #374151",
              borderRadius: "0.5rem",
              color: "#f3f4f6",
            }}
            formatter={(value, _name, item) => [
              formatMoney(value),
              `P&L (${item?.payload?.trades ?? 0} trades)`,
            ]}
          />
          <Bar dataKey="pnl" radius={[2, 2, 0, 0]}>
            {chartData.map((d, i) => (
              <Cell key={i} fill={d.pnl >= 0 ? "#34d399" : "#f87171"} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
