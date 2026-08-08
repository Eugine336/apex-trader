// A compact stat tile for the department pages' summary rows. Monospace value,
// uppercase micro-label — institutional density.
export default function StatTile({ label, value, accent = "text-gray-100", sub = null }) {
  return (
    <div className="rounded-md border border-gray-700/70 bg-gray-850/60 px-3 py-2.5">
      <div className="text-[10px] font-medium uppercase tracking-[0.08em] text-gray-500">
        {label}
      </div>
      <div className={`num mt-1 text-lg font-semibold leading-none ${accent}`}>
        {value}
      </div>
      {sub && <div className="mt-1 text-[11px] text-gray-500">{sub}</div>}
    </div>
  );
}
