// Maps an instance status string to a colored dot + label.
const STATUS_STYLES = {
  RUNNING: { dot: "bg-emerald-500", text: "text-emerald-400", label: "Running" },
  STARTING: { dot: "bg-yellow-500", text: "text-yellow-400", label: "Starting" },
  STOPPING: { dot: "bg-yellow-500", text: "text-yellow-400", label: "Stopping" },
  CRASHED: { dot: "bg-red-500", text: "text-red-400", label: "Crashed" },
  STOPPED: { dot: "bg-gray-500", text: "text-gray-400", label: "Stopped" },
};

export default function StatusBadge({ status, showLabel = true, size = "sm" }) {
  const key = String(status || "STOPPED").toUpperCase();
  const style = STATUS_STYLES[key] || STATUS_STYLES.STOPPED;
  const dotSize = size === "lg" ? "h-3 w-3" : "h-2.5 w-2.5";
  const pulse = key === "RUNNING" || key === "STARTING" ? "animate-pulse" : "";

  return (
    <span className="inline-flex items-center gap-2">
      <span className={`${dotSize} rounded-full ${style.dot} ${pulse}`} />
      {showLabel && (
        <span className={`text-sm font-medium ${style.text}`}>
          {style.label}
        </span>
      )}
    </span>
  );
}
