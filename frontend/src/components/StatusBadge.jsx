// Maps an instance status string to a colored dot + label.
const STATUS_STYLES = {
  RUNNING: { dot: "bg-emerald-500", text: "text-emerald-400", label: "Running" },
  STARTING: { dot: "bg-amber-400", text: "text-amber-300", label: "Starting" },
  STOPPING: { dot: "bg-amber-400", text: "text-amber-300", label: "Stopping" },
  CRASHED: { dot: "bg-red-500", text: "text-red-400", label: "Crashed" },
  STOPPED: { dot: "bg-gray-500", text: "text-gray-400", label: "Stopped" },
};

export default function StatusBadge({ status, showLabel = true, size = "sm" }) {
  const key = String(status || "STOPPED").toUpperCase();
  const style = STATUS_STYLES[key] || STATUS_STYLES.STOPPED;
  const dotSize = size === "lg" ? "h-2.5 w-2.5" : "h-2 w-2";
  const live = key === "RUNNING" || key === "STARTING";

  return (
    <span className="inline-flex items-center gap-2">
      <span className={`${dotSize} rounded-full ${style.dot} ${live ? "pulse-dot" : ""}`} />
      {showLabel && (
        <span
          className={`text-[11px] font-semibold uppercase tracking-[0.06em] ${style.text}`}
        >
          {style.label}
        </span>
      )}
    </span>
  );
}
