// A single stat tile for the dashboard top row.
export default function SummaryCard({ label, value, accent = "text-gray-100", icon = null, sub = null }) {
  return (
    <div className="rounded-lg border border-gray-700 bg-gray-800 p-5 shadow-lg">
      <div className="flex items-center justify-between">
        <p className="text-sm font-medium text-gray-400">{label}</p>
        {icon}
      </div>
      <p className={`mt-2 text-2xl font-semibold ${accent}`}>{value}</p>
      {sub && <p className="mt-1 text-xs text-gray-500">{sub}</p>}
    </div>
  );
}
