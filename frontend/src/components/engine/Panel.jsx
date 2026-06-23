// A titled card matching the new frontend's panel styling.
export default function Panel({ title, subtitle, children, className = "" }) {
  return (
    <div
      className={`rounded-lg border border-gray-700 bg-gray-800 p-5 shadow-lg ${className}`}
    >
      {(title || subtitle) && (
        <div className="mb-4">
          {title && (
            <h2 className="text-sm font-semibold text-gray-100">{title}</h2>
          )}
          {subtitle && <p className="mt-0.5 text-xs text-gray-500">{subtitle}</p>}
        </div>
      )}
      {children}
    </div>
  );
}
