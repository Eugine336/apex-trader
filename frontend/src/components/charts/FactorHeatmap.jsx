// Factor heatmap for the Scanner page — top 20 instruments by score, with a
// per-factor intensity grid. Ported from the original dashboard, re-styled for
// the Tailwind dark theme.

const FACTORS = ["structure", "fvg", "ob", "liquidity", "sweep", "session", "strength"];

function cellStyle(v) {
  if (v >= 8) return { background: "rgba(16,185,129,0.55)", color: "#d1fae5" };
  if (v >= 5) return { background: "rgba(16,185,129,0.30)", color: "#6ee7b7" };
  if (v >= 3) return { background: "rgba(16,185,129,0.15)", color: "#34d399" };
  return { background: "rgba(255,255,255,0.04)", color: "#6b7280" };
}

export default function FactorHeatmap({ instruments }) {
  const list = instruments || [];
  if (!list.length) {
    return (
      <div className="py-6 text-center text-xs text-gray-500">No scan data yet.</div>
    );
  }
  const top = [...list].sort((a, b) => (b.score || 0) - (a.score || 0)).slice(0, 20);
  const cols = "80px repeat(7, 1fr) 56px";

  return (
    <div className="overflow-x-auto">
      <div className="min-w-[560px]">
        <div
          className="mb-1 grid items-center gap-1 text-[10px] uppercase text-gray-500"
          style={{ gridTemplateColumns: cols }}
        >
          <div>Symbol</div>
          {FACTORS.map((f) => (
            <div key={f} className="text-center">
              {f}
            </div>
          ))}
          <div className="text-center">Score</div>
        </div>
        {top.map((inst) => (
          <div
            key={inst.symbol}
            className="mb-1 grid items-center gap-1"
            style={{ gridTemplateColumns: cols }}
          >
            <div className="truncate text-xs font-semibold text-gray-200">
              {inst.symbol}
            </div>
            {FACTORS.map((f) => {
              const v = inst.factors?.[f] ?? 0;
              return (
                <div
                  key={f}
                  className="rounded py-1 text-center text-xs font-medium"
                  style={cellStyle(v)}
                >
                  {v}
                </div>
              );
            })}
            <div className="text-center text-xs font-bold text-gray-100">
              {inst.score}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
