// Page heading for the engine (department) pages.
export function EngineHeader({ title, subtitle, actions = null }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-gray-100">
          {title}
        </h1>
        {subtitle && <p className="mt-1 text-sm text-gray-400">{subtitle}</p>}
      </div>
      {actions && <div className="shrink-0">{actions}</div>}
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
      <div className="rounded-lg border border-amber-500/30 bg-amber-500/[0.07] px-4 py-8 text-center">
        <p className="text-sm font-medium text-amber-300">
          Your trading instance is not running.
        </p>
        <p className="mt-1 text-xs text-gray-400">
          Start it from the <span className="font-semibold">Instance</span> page
          to stream live engine data.
        </p>
      </div>
    );
  }
  if (loading && !hasData) {
    return (
      <div className="flex items-center gap-2 text-sm text-gray-400">
        <span className="h-2 w-2 animate-pulse rounded-full bg-accent" />
        Loading engine data…
      </div>
    );
  }
  return null;
}
