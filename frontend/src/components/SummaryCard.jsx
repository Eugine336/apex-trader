// A single metric tile for the dashboard / command-center top row.
export default function SummaryCard({
  label,
  value,
  accent = "text-gray-100",
  icon = null,
  sub = null,
}) {
  return (
    <div className="rounded-lg border border-gray-700/80 bg-gray-800/80 px-4 py-3.5">
      <div className="flex items-center justify-between">
        <p className="text-[11px] font-medium uppercase tracking-[0.08em] text-gray-400">
          {label}
        </p>
        {icon}
      </div>
      <p className={`num mt-2 text-2xl font-semibold leading-none ${accent}`}>
        {value}
      </p>
      {sub && <p className="mt-1.5 text-[11px] text-gray-500">{sub}</p>}
    </div>
  );
}
