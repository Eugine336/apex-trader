import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

// Trade-volume (count per day) bar chart. `data` is a daily-pnl list
// [{ date, pnl, trades }] (oldest → newest); only the trade count is plotted.
export default function TradeVolumeChart({ data = [] }) {
  if (!data.length) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-gray-500">
        No trade volume yet.
      </div>
    );
  }

  const chartData = data.map((p) => ({
    date: p.date,
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
            allowDecimals={false}
            width={40}
          />
          <Tooltip
            cursor={{ fill: "#37415133" }}
            contentStyle={{
              backgroundColor: "#1f2937",
              border: "1px solid #374151",
              borderRadius: "0.5rem",
              color: "#f3f4f6",
            }}
            formatter={(value) => [value, "Trades"]}
          />
          <Bar dataKey="trades" fill="#60a5fa" radius={[2, 2, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
