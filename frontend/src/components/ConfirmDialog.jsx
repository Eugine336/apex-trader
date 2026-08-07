// Generic confirmation modal. Renders nothing when `open` is false.
export default function ConfirmDialog({
  open,
  title = "Are you sure?",
  message = "",
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  destructive = false,
  loading = false,
  onConfirm,
  onCancel,
}) {
  if (!open) return null;

  const confirmClass = destructive
    ? "bg-red-600 hover:bg-red-700"
    : "bg-emerald-600 hover:bg-emerald-700";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-md rounded-lg border border-gray-700 bg-gray-800 p-6 shadow-2xl">
        <h3 className="text-lg font-semibold text-gray-100">{title}</h3>
        {message && <p className="mt-2 text-sm text-gray-400">{message}</p>}
        <div className="mt-6 flex justify-end gap-3">
          <button
            type="button"
            onClick={onCancel}
            disabled={loading}
            className="rounded-md border border-gray-600 px-4 py-2 text-sm font-medium text-gray-300 transition-colors duration-200 hover:bg-gray-700 disabled:opacity-50"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={loading}
            className={`rounded-md px-4 py-2 text-sm font-medium text-white transition-colors duration-200 disabled:opacity-50 ${confirmClass}`}
          >
            {loading ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
