// Small formatting helpers shared across pages/components.

export function formatMoney(value) {
  const n = Number(value || 0);
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toFixed(2)}`;
}

// win_rate from the API is a fraction (0–1); render as a percentage.
export function formatPercent(fraction, digits = 1) {
  return `${(Number(fraction || 0) * 100).toFixed(digits)}%`;
}

export function formatDateTime(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString();
}

export function pnlColor(value) {
  const n = Number(value || 0);
  if (n > 0) return "text-emerald-400";
  if (n < 0) return "text-red-400";
  return "text-gray-300";
}

export function directionBadgeClass(direction) {
  const dir = String(direction || "").toUpperCase();
  if (dir === "LONG" || dir === "BUY") {
    return "bg-emerald-500/15 text-emerald-400 border border-emerald-500/30";
  }
  if (dir === "SHORT" || dir === "SELL") {
    return "bg-red-500/15 text-red-400 border border-red-500/30";
  }
  return "bg-gray-600/30 text-gray-300 border border-gray-600";
}
