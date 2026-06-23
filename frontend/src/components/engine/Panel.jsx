// A titled surface card — the institutional panel primitive used across every
// department page. Sharp edges, hairline border, subtle elevation.
//
// Pass `flush` (or `className="p-0"`, kept for backward compatibility) to drop
// the inner body padding for edge-to-edge tables.
export default function Panel({
  title,
  subtitle,
  actions = null,
  children,
  flush = false,
  className = "",
}) {
  const noPad = flush || /\bp-0\b/.test(className);
  return (
    <div
      className={`rounded-lg border border-gray-700/80 bg-gray-800/80 shadow-[0_1px_0_0_rgba(255,255,255,0.02)_inset,0_2px_8px_-2px_rgba(0,0,0,0.5)] ${className}`}
    >
      {(title || subtitle || actions) && (
        <div className="flex items-start justify-between gap-3 border-b border-gray-700/70 px-4 py-3">
          <div className="min-w-0">
            {title && (
              <h2 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-gray-300">
                {title}
              </h2>
            )}
            {subtitle && (
              <p className="mt-0.5 truncate text-xs text-gray-500">{subtitle}</p>
            )}
          </div>
          {actions && <div className="shrink-0">{actions}</div>}
        </div>
      )}
      <div className={noPad ? "" : "p-4"}>{children}</div>
    </div>
  );
}
