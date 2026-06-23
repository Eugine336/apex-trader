// Page heading for the engine (Intelligence & Consensus) pages.
export function EngineHeader({ title, subtitle }) {
  return (
    <div>
      <h1 className="text-2xl font-semibold text-gray-100">{title}</h1>
      {subtitle && <p className="mt-1 text-sm text-gray-400">{subtitle}</p>}
    </div>
  );
}

// Renders the appropriate banner for the engine data lifecycle, or null when
// data is available. `unavailable` means the user's instance is not running.
export function EngineNotice({ loading, unavailable, error, hasData }) {
  if (error) {
    return (
      <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-400">
        {error}
      </div>
    );
  }
  if (unavailable) {
    return (
      <div className="rounded-md border border-yellow-500/40 bg-yellow-500/10 px-4 py-6 text-center text-sm text-yellow-300">
        Your trading instance is not running. Start it from the{" "}
        <span className="font-semibold">Instance</span> page to view live engine
        data.
      </div>
    );
  }
  if (loading && !hasData) {
    return (
      <div className="animate-pulse text-sm text-gray-400">
        Loading engine data…
      </div>
    );
  }
  return null;
}
