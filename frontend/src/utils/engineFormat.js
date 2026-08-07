// Shared formatting + color helpers for the engine (Intelligence & Consensus)
// pages. Centralizes the green/red/neutral conventions the original dashboard
// expressed via CSS variables, mapped onto the new Tailwind dark theme.

export function dirText(direction) {
  const v = String(direction || "").toUpperCase();
  if (v === "LONG" || v === "BUY") return "text-emerald-400";
  if (v === "SHORT" || v === "SELL") return "text-red-400";
  return "text-gray-400";
}

export function dirCellClass(direction) {
  const v = String(direction || "").toUpperCase();
  if (v === "LONG") return "bg-emerald-500/15 text-emerald-400";
  if (v === "SHORT") return "bg-red-500/15 text-red-400";
  return "text-gray-500";
}

export function evText(ev) {
  const n = Number(ev || 0);
  if (n > 0.5) return "text-emerald-400";
  if (n < 0) return "text-red-400";
  return "text-yellow-400";
}

export function sizeText(m) {
  const n = Number(m || 0);
  if (n >= 0.9) return "text-emerald-400";
  if (n >= 0.7) return "text-yellow-400";
  if (n <= 0) return "text-red-400";
  return "text-sky-400";
}

// Color for a signed/bounded situation dimension. When min < 0 the dimension
// is signed (e.g. alignment in [-1,1]); otherwise it is a 0..1 quality score.
export function dimText(val, min = -1) {
  if (val == null) return "text-gray-500";
  const n = Number(val);
  if (min < 0) {
    if (n > 0.3) return "text-emerald-400";
    if (n < -0.3) return "text-red-400";
    return "text-yellow-400";
  }
  if (n > 0.7) return "text-emerald-400";
  if (n < 0.4) return "text-red-400";
  return "text-yellow-400";
}

export function dimBgHex(val, min = -1) {
  if (val == null) return "#6b7280"; // gray-500
  const n = Number(val);
  if (min < 0) {
    if (n > 0.3) return "#34d399";
    if (n < -0.3) return "#f87171";
    return "#facc15";
  }
  if (n > 0.7) return "#34d399";
  if (n < 0.4) return "#f87171";
  return "#facc15";
}

export function dimBarPct(val, min, max) {
  if (val == null) return 0;
  const n = Number(val);
  return Math.max(0, Math.min(100, ((n - min) / (max - min)) * 100));
}

export function fmtSigned(v, digits = 3) {
  if (v == null) return "—";
  const n = Number(v);
  return (n >= 0 ? "+" : "") + n.toFixed(digits);
}

export function fmtTs(ts) {
  if (!ts) return "—";
  try {
    const d = new Date(ts);
    return (
      d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" }) +
      " " +
      d.toLocaleTimeString("en-GB", {
        hour12: false,
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    );
  } catch {
    return "—";
  }
}

export function truncate(s, len) {
  if (!s) return "—";
  return s.length > len ? s.slice(0, len) + "…" : s;
}

// Tailwind classes for the small status/action pills used across the pages.
const BADGE = {
  green: "bg-emerald-500/10 text-emerald-400 ring-1 ring-inset ring-emerald-500/25",
  red: "bg-red-500/10 text-red-400 ring-1 ring-inset ring-red-500/25",
  yellow: "bg-amber-500/10 text-amber-300 ring-1 ring-inset ring-amber-500/25",
  blue: "bg-sky-500/10 text-sky-300 ring-1 ring-inset ring-sky-500/25",
  orange: "bg-orange-500/10 text-orange-300 ring-1 ring-inset ring-orange-500/25",
  purple: "bg-purple-500/10 text-purple-300 ring-1 ring-inset ring-purple-500/25",
  muted: "bg-gray-700/50 text-gray-300 ring-1 ring-inset ring-gray-600/40",
};

export function badgeClass(variant) {
  return `inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.04em] ${
    BADGE[variant] || BADGE.muted
  }`;
}

const ACTION_BADGES = {
  HOLD: "green",
  CLOSE: "red",
  TIGHTEN_SL: "yellow",
  MOVE_TO_BREAKEVEN: "blue",
  TRAIL: "blue",
  PARTIAL_CLOSE: "orange",
  SCALE_IN: "purple",
  ENTER_MARKET: "green",
  ENTER_PENDING: "green",
  SKIP: "muted",
  OBSERVE: "muted",
  SET_PROTECTIVE_STOP: "yellow",
};

export function actionBadgeVariant(action) {
  return ACTION_BADGES[String(action || "").toUpperCase()] || "muted";
}
