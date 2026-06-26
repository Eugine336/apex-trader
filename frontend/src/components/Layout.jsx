import { useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";

import { useAuth } from "../context/AuthContext";
import { RealtimeProvider, useRealtime } from "../context/RealtimeContext";
import { formatMoney, formatPercent, pnlColor } from "../utils/format";

// Inline icons (no icon dependency).
const icons = {
  command: <path d="M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z" />,
  dashboard: (
    <path d="M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z" />
  ),
  trades: <path d="M3 5h18v2H3V5zm0 6h18v2H3v-2zm0 6h18v2H3v-2z" />,
  positions: (
    <path d="M3 3v18h18v-2H5V3H3zm14 5l-4 4-2-2-4 4 1.41 1.41L11 13l2 2 5-5L17 8z" />
  ),
  broker: (
    <path d="M12 1 3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4z" />
  ),
  config: (
    <path d="M19.14 12.94a7.49 7.49 0 0 0 0-1.88l2.03-1.58-2-3.46-2.39.96a7.03 7.03 0 0 0-1.62-.94L14.8 2.4h-4l-.36 2.64c-.58.24-1.12.56-1.62.94l-2.39-.96-2 3.46 2.03 1.58a7.49 7.49 0 0 0 0 1.88L4.43 14.5l2 3.46 2.39-.96c.5.38 1.04.7 1.62.94l.36 2.66h4l.36-2.64c.58-.24 1.12-.56 1.62-.94l2.39.96 2-3.46-2.03-1.58zM12 15.6A3.6 3.6 0 1 1 12 8.4a3.6 3.6 0 0 1 0 7.2z" />
  ),
  instance: (
    <path d="M13 3v2h4.59l-9.83 9.83 1.41 1.41L19 6.41V11h2V3h-8zM5 5h6V3H3v18h18v-8h-2v6H5V5z" />
  ),
  admin: (
    <path d="M12 1 3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm-1 6h2v2h-2V7zm0 4h2v6h-2v-6z" />
  ),
  users: (
    <path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z" />
  ),
  monitor: (
    <path d="M21 3H3c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h6v2h6v-2h6c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 14H3V5h18v12z" />
  ),
  scanner: (
    <path d="M15.5 14h-.79l-.28-.27a6.5 6.5 0 1 0-.7.7l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0A4.5 4.5 0 1 1 14 9.5 4.5 4.5 0 0 1 9.5 14z" />
  ),
  votes: <path d="M3 3h8v8H3V3zm10 0h8v8h-8V3zM3 13h8v8H3v-8zm10 0h8v8h-8v-8z" />,
  ranker: <path d="M5 9.2h3V19H5V9.2zM10.6 5h2.8v14h-2.8V5zm5.6 8H19v6h-2.8v-6z" />,
  decisions: <path d="M3 5h18v2H3V5zm0 6h18v2H3v-2zm0 6h12v2H3v-2z" />,
  trace: (
    <path d="M4 4h4v4H4V4zm6 1h10v2H10V5zM4 10h4v4H4v-4zm6 1h10v2H10v-2zM4 16h4v4H4v-4zm6 1h10v2H10v-2z" />
  ),
  orchestrator: (
    <path d="M12 2a2 2 0 0 1 2 2 2 2 0 0 1-1 1.73V8h3a3 3 0 0 1 3 3v1.27A2 2 0 0 1 20 14a2 2 0 1 1-2.73-1.86V11a1 1 0 0 0-1-1h-3v2.27a2 2 0 1 1-2 0V10H8a1 1 0 0 0-1 1v1.14A2 2 0 1 1 4 14a2 2 0 0 1 1-1.73V11a3 3 0 0 1 3-3h3V5.73A2 2 0 0 1 10 4a2 2 0 0 1 2-2z" />
  ),
  risk: (
    <path d="M12 1 3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm-1 5h2v6h-2V6zm0 8h2v2h-2v-2z" />
  ),
  governor: (
    <path d="M12 2 4 5v6c0 5 3.4 9.7 8 11 4.6-1.3 8-6 8-11V5l-8-3zm0 5a3 3 0 0 1 3 3c0 1.3-.8 2.4-2 2.8V16h-2v-3.2c-1.2-.4-2-1.5-2-2.8a3 3 0 0 1 3-3z" />
  ),
  planner: (
    <path d="M19 3h-1V1h-2v2H8V1H6v2H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2zm0 16H5V9h14v10zM7 11h5v5H7v-5z" />
  ),
  operations: (
    <path d="M19.14 12.94a7.49 7.49 0 0 0 0-1.88l2.03-1.58-2-3.46-2.39.96a7.03 7.03 0 0 0-1.62-.94L14.8 2.4h-4l-.36 2.64c-.58.24-1.12.56-1.62.94l-2.39-.96-2 3.46 2.03 1.58a7.49 7.49 0 0 0 0 1.88L4.43 14.5l2 3.46 2.39-.96c.5.38 1.04.7 1.62.94l.36 2.66h4l.36-2.64c.58-.24 1.12-.56 1.62-.94l2.39.96 2-3.46-2.03-1.58zM12 15.6A3.6 3.6 0 1 1 12 8.4a3.6 3.6 0 0 1 0 7.2z" />
  ),
  activeTrades: (
    <path d="M3.5 18.49l6-6.01 4 4L22 6.92l-1.41-1.41-7.09 7.97-4-4L2 16.99z" />
  ),
  health: (
    <path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z" />
  ),
  history: (
    <path d="M13 3a9 9 0 0 0-9 9H1l3.89 3.89.07.14L9 12H6a7 7 0 1 1 7 7c-1.93 0-3.68-.79-4.94-2.06l-1.42 1.42A8.97 8.97 0 0 0 13 21a9 9 0 0 0 0-18zm-1 5v5l4.28 2.54.72-1.21-3.5-2.08V8H12z" />
  ),
  learning: (
    <path d="M12 3 1 9l11 6 9-4.91V17h2V9L12 3zM5 13.18v4L12 21l7-3.82v-4L12 17l-7-3.82z" />
  ),
  feedback: (
    <path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-7 12h-2v-2h2v2zm0-4h-2V6h2v4z" />
  ),
  ml: (
    <path d="M9 2a3 3 0 0 0-3 3 3 3 0 0 0 0 .27A3 3 0 0 0 4 8a3 3 0 0 0 1 2.22V11a3 3 0 0 0 0 5.66V17a3 3 0 1 0 5.83 1H12V4.83A3 3 0 0 0 9 2zm6 0a3 3 0 0 0-3 2.83V18h1.17A3 3 0 1 0 19 17v-.34A3 3 0 0 0 20 11v-.78A3 3 0 0 0 20 5.27 3 3 0 0 0 18 2.27 3 3 0 0 0 15 2z" />
  ),
  evolution: (
    <path d="M3 17h2v-2H3v2zm0-4h2V5H3v8zm4 4h2V9H7v8zm0-10h2V5H7v2zm4 10h2V3h-2v14zm4 0h2v-6h-2v6zm0-8h2V5h-2v4zm4 8h2V7h-2v10z" />
  ),
  shadow: (
    <path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm0 18V4a8 8 0 0 1 0 16z" />
  ),
  reconciliation: (
    <path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z" />
  ),
  moduleGovernor: (
    <path d="M12 2 4 6v6c0 5 3.4 9.4 8 10 4.6-.6 8-5 8-10V6l-8-4zm0 4a2 2 0 1 1 0 4 2 2 0 0 1 0-4zm0 6c2 0 4 1 4 3v1H8v-1c0-2 2-3 4-3z" />
  ),
  logout: (
    <path d="M16 13v-2H7V8l-5 4 5 4v-3h9zm3-10H10a2 2 0 0 0-2 2v3h2V5h9v14h-9v-3H8v3a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2z" />
  ),
  menu: <path d="M3 6h18v2H3V6zm0 5h18v2H3v-2zm0 5h18v2H3v-2z" />,
  more: (
    <path d="M6 10c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm12 0c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm-6 0c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z" />
  ),
  close: (
    <path d="M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" />
  ),
};

function Icon({ name, className = "h-[18px] w-[18px]" }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={className}>
      {icons[name]}
    </svg>
  );
}

// Core nav — always visible.
const coreItems = [
  { to: "/", label: "Command Center", icon: "command", end: true },
  { to: "/trades", label: "Trades", icon: "trades" },
  { to: "/positions", label: "Positions", icon: "positions" },
];

// The 9-department signal flow. Engine groups only render while the instance
// runs; each carries a `dept` key matching the live snapshot health map.
const departmentGroups = [
  {
    dept: "intelligence",
    label: "① Intelligence",
    items: [
      { to: "/engine/scanner", label: "Scanner", icon: "scanner" },
      { to: "/engine/votes", label: "Module Votes", icon: "votes" },
      { to: "/engine/market-model", label: "Market Model", icon: "scanner" },
    ],
  },
  {
    dept: "consensus",
    label: "② Consensus",
    items: [
      { to: "/engine/ranker", label: "Ranker", icon: "ranker" },
      { to: "/engine/decisions", label: "Decisions", icon: "decisions" },
      { to: "/engine/trace", label: "Decision Trace", icon: "trace" },
      { to: "/engine/orchestrator", label: "Orchestrator", icon: "orchestrator" },
    ],
  },
  {
    dept: "compliance",
    label: "③ Compliance",
    items: [
      { to: "/engine/risk", label: "Risk Monitor", icon: "risk" },
      { to: "/engine/governor", label: "Governor", icon: "governor" },
    ],
  },
  {
    dept: "portfolio",
    label: "④ Portfolio",
    items: [{ to: "/engine/planner", label: "Planner", icon: "planner" }],
  },
  {
    dept: "execution",
    label: "⑤ Execution",
    items: [
      { to: "/engine/active-trades", label: "Active Trades", icon: "activeTrades" },
      { to: "/engine/position-health", label: "Position Health", icon: "health" },
      { to: "/engine/history", label: "Trade History", icon: "history" },
    ],
  },
  {
    dept: "operations",
    label: "⑥ Operations",
    items: [{ to: "/engine/operations", label: "Operations", icon: "operations" }],
  },
  {
    dept: "learning",
    label: "⑦ Learning",
    items: [
      { to: "/engine/learning", label: "Learning Layer", icon: "learning" },
      { to: "/engine/adaptive-learning", label: "Adaptive Learning", icon: "learning" },
      { to: "/engine/feedback", label: "Outcome Feedback", icon: "feedback" },
      { to: "/engine/ml", label: "ML Insights", icon: "ml" },
      { to: "/engine/evolution", label: "Evolution", icon: "evolution" },
    ],
  },
  {
    dept: "governance",
    label: "⑧ Governance",
    items: [
      { to: "/engine/shadow", label: "Shadow Outcomes", icon: "shadow" },
      { to: "/engine/reconciliation", label: "Reconciliation", icon: "reconciliation" },
      { to: "/engine/module-governor", label: "Module Governor", icon: "moduleGovernor" },
    ],
  },
];

const settingsItems = [
  { to: "/settings/broker", label: "Broker", icon: "broker" },
  { to: "/settings/config", label: "Config", icon: "config" },
  { to: "/settings/instance", label: "Instance", icon: "instance" },
];

const adminItems = [
  { to: "/admin", label: "Overview", icon: "admin", end: true },
  { to: "/admin/users", label: "Users", icon: "users" },
  { to: "/admin/instances", label: "Instances", icon: "monitor" },
  { to: "/admin/trades", label: "All Trades", icon: "trades" },
];

// Mobile bottom-tab targets.
const bottomTabs = [
  { to: "/", label: "Command", icon: "command", end: true },
  { to: "/engine/operations", label: "Ops", icon: "operations" },
  { to: "/trades", label: "Trades", icon: "trades" },
  { to: "/engine/scanner", label: "Intel", icon: "scanner" },
];

const RUNNING_STATES = new Set(["RUNNING", "STARTING"]);

const DEPT_DOT = {
  active: "bg-emerald-500",
  degraded: "bg-amber-400",
  error: "bg-red-500",
  offline: "bg-gray-600",
};

const navLinkClass = ({ isActive }) =>
  `flex items-center gap-3 rounded-md px-3 py-2 text-[13px] font-medium transition-colors ${
    isActive
      ? "bg-accent/10 text-accent-soft ring-1 ring-inset ring-accent/30"
      : "text-gray-400 hover:bg-gray-700/50 hover:text-gray-100"
  }`;

function NavSection({ label, dot }) {
  return (
    <div className="flex items-center gap-2 px-3 pb-1 pt-4">
      {dot && <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />}
      <p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-gray-600">
        {label}
      </p>
    </div>
  );
}

function ConnectionPill({ connected, transport }) {
  const label =
    transport === "sse" ? "LIVE" : transport === "poll" ? "POLL" : "OFF";
  const tone = connected
    ? transport === "sse"
      ? "text-emerald-400"
      : "text-amber-300"
    : "text-gray-500";
  const dot = connected
    ? transport === "sse"
      ? "bg-emerald-500 pulse-dot"
      : "bg-amber-400"
    : "bg-gray-600";
  return (
    <span className="inline-flex items-center gap-1.5" title={`Realtime: ${label}`}>
      <span className={`h-2 w-2 rounded-full ${dot}`} />
      <span className={`text-[10px] font-semibold tracking-[0.08em] ${tone}`}>
        {label}
      </span>
    </span>
  );
}

function TickerMetric({ label, value, accent = "text-gray-100" }) {
  return (
    <div className="flex flex-col leading-tight">
      <span className="text-[9px] font-medium uppercase tracking-[0.1em] text-gray-500">
        {label}
      </span>
      <span className={`num text-sm font-semibold ${accent}`}>{value}</span>
    </div>
  );
}

function Sidebar({ user, running, deptState, onNavigate, onLogout }) {
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2.5 px-4 py-4">
        <div className="flex h-8 w-8 items-center justify-center rounded bg-accent font-bold text-white">
          A
        </div>
        <div className="leading-tight">
          <div className="text-sm font-semibold tracking-tight text-gray-100">
            APEX Trader
          </div>
          <div className="text-[10px] uppercase tracking-[0.14em] text-gray-500">
            Trading Desk
          </div>
        </div>
      </div>

      <nav className="apex-scroll flex-1 space-y-0.5 overflow-y-auto px-2.5 pb-4">
        {coreItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={navLinkClass}
            onClick={onNavigate}
          >
            <Icon name={item.icon} />
            {item.label}
          </NavLink>
        ))}

        {running &&
          departmentGroups.map((group) => (
            <div key={group.dept}>
              <NavSection
                label={group.label}
                dot={DEPT_DOT[deptState[group.dept]] || DEPT_DOT.offline}
              />
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={navLinkClass}
                  onClick={onNavigate}
                >
                  <Icon name={item.icon} />
                  {item.label}
                </NavLink>
              ))}
            </div>
          ))}

        <NavSection label="Settings" />
        {settingsItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={navLinkClass}
            onClick={onNavigate}
          >
            <Icon name={item.icon} />
            {item.label}
          </NavLink>
        ))}

        {user?.is_admin && (
          <>
            <NavSection label="Admin" />
            {adminItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={navLinkClass}
                onClick={onNavigate}
              >
                <Icon name={item.icon} />
                {item.label}
              </NavLink>
            ))}
          </>
        )}
      </nav>

      <div className="border-t border-gray-700/70 p-3">
        <p className="truncate text-xs text-gray-400" title={user?.email}>
          {user?.email}
        </p>
        <button
          type="button"
          onClick={onLogout}
          className="mt-2 flex w-full items-center gap-2 rounded-md px-3 py-2 text-[13px] font-medium text-gray-400 transition-colors hover:bg-gray-700/50 hover:text-red-400"
        >
          <Icon name="logout" />
          Logout
        </button>
      </div>
    </div>
  );
}

function LayoutShell() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const { snapshot, connected, transport } = useRealtime();
  const [drawerOpen, setDrawerOpen] = useState(false);

  const status = snapshot?.instance?.status || "STOPPED";
  const running = RUNNING_STATES.has(String(status).toUpperCase());

  const deptState = {};
  (snapshot?.departments || []).forEach((d) => {
    deptState[d.key] = d.state;
  });

  const summary = snapshot?.summary || {};
  const totalPnl = summary.total_pnl ?? 0;
  const openPnl = snapshot?.open_pnl ?? 0;
  const openCount = snapshot?.open_positions_count ?? 0;
  const winRate = summary.win_rate ?? 0;

  const handleLogout = () => {
    logout();
    navigate("/login", { replace: true });
  };

  return (
    <div className="flex h-full bg-gray-900 text-gray-100">
      {/* Desktop sidebar */}
      <aside className="hidden w-60 shrink-0 border-r border-gray-700/70 bg-gray-850 lg:block">
        <Sidebar
          user={user}
          running={running}
          deptState={deptState}
          onNavigate={() => {}}
          onLogout={handleLogout}
        />
      </aside>

      {/* Mobile slide-over drawer (full nav) */}
      {drawerOpen && (
        <div className="fixed inset-0 z-50 lg:hidden">
          <div
            className="absolute inset-0 bg-black/70"
            onClick={() => setDrawerOpen(false)}
          />
          <aside className="absolute right-0 top-0 h-full w-72 border-l border-gray-700/70 bg-gray-850 shadow-2xl">
            <div className="flex justify-end p-2">
              <button
                type="button"
                onClick={() => setDrawerOpen(false)}
                className="rounded p-1 text-gray-400 hover:text-gray-100"
                aria-label="Close navigation"
              >
                <Icon name="close" className="h-6 w-6" />
              </button>
            </div>
            <div className="h-[calc(100%-3rem)]">
              <Sidebar
                user={user}
                running={running}
                deptState={deptState}
                onNavigate={() => setDrawerOpen(false)}
                onLogout={handleLogout}
              />
            </div>
          </aside>
        </div>
      )}

      {/* Main column */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center justify-between gap-3 border-b border-gray-700/70 bg-gray-850/80 px-3 backdrop-blur md:px-5">
          <div className="flex min-w-0 items-center gap-2.5">
            <div className="flex h-7 w-7 items-center justify-center rounded bg-accent text-sm font-bold text-white lg:hidden">
              A
            </div>
            {/* Live metrics ticker */}
            <div className="flex items-center gap-4 overflow-x-auto md:gap-6">
              <TickerMetric
                label="Total P&L"
                value={formatMoney(totalPnl)}
                accent={pnlColor(totalPnl)}
              />
              <TickerMetric
                label="Open P&L"
                value={formatMoney(openPnl)}
                accent={pnlColor(openPnl)}
              />
              <TickerMetric label="Open" value={openCount} />
              <div className="hidden sm:block">
                <TickerMetric label="Win Rate" value={formatPercent(winRate)} />
              </div>
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-3">
            <ConnectionPill connected={connected} transport={transport} />
            <span className="hidden items-center gap-1.5 sm:inline-flex">
              <span
                className={`h-2 w-2 rounded-full ${
                  running ? "bg-emerald-500 pulse-dot" : "bg-gray-600"
                }`}
              />
              <span
                className={`text-[11px] font-semibold uppercase tracking-[0.06em] ${
                  running ? "text-emerald-400" : "text-gray-400"
                }`}
              >
                {String(status)}
              </span>
            </span>
            <button
              type="button"
              className="rounded p-1 text-gray-400 hover:text-gray-100 lg:hidden"
              onClick={() => setDrawerOpen(true)}
              aria-label="Open navigation"
            >
              <Icon name="menu" className="h-6 w-6" />
            </button>
          </div>
        </header>

        <main className="apex-scroll has-bottom-nav flex-1 overflow-y-auto p-3 md:p-5">
          <Outlet />
        </main>

        {/* Mobile bottom tab bar */}
        <nav className="fixed inset-x-0 bottom-0 z-40 flex h-16 items-stretch border-t border-gray-700/70 bg-gray-850/95 backdrop-blur lg:hidden">
          {bottomTabs.map((tab) => (
            <NavLink
              key={tab.to}
              to={tab.to}
              end={tab.end}
              className={({ isActive }) =>
                `flex flex-1 flex-col items-center justify-center gap-1 text-[10px] font-medium ${
                  isActive ? "text-accent-soft" : "text-gray-400"
                }`
              }
            >
              <Icon name={tab.icon} className="h-5 w-5" />
              {tab.label}
            </NavLink>
          ))}
          <button
            type="button"
            onClick={() => setDrawerOpen(true)}
            className="flex flex-1 flex-col items-center justify-center gap-1 text-[10px] font-medium text-gray-400"
          >
            <Icon name="more" className="h-5 w-5" />
            More
          </button>
        </nav>
      </div>
    </div>
  );
}

export default function Layout() {
  return (
    <RealtimeProvider>
      <LayoutShell />
    </RealtimeProvider>
  );
}
