import { useCallback, useEffect, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";

import { getStatus } from "../api/trading";
import { useAuth } from "../context/AuthContext";
import StatusBadge from "./StatusBadge";

// Inline icons (no icon dependency).
const icons = {
  dashboard: (
    <path d="M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z" />
  ),
  trades: (
    <path d="M3 5h18v2H3V5zm0 6h18v2H3v-2zm0 6h18v2H3v-2z" />
  ),
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
  logout: (
    <path d="M16 13v-2H7V8l-5 4 5 4v-3h9zm3-10H10a2 2 0 0 0-2 2v3h2V5h9v14h-9v-3H8v3a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2z" />
  ),
  menu: <path d="M3 6h18v2H3V6zm0 5h18v2H3v-2zm0 5h18v2H3v-2z" />,
  close: (
    <path d="M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" />
  ),
};

function Icon({ name, className = "h-5 w-5" }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={className}>
      {icons[name]}
    </svg>
  );
}

const navItems = [
  { to: "/", label: "Dashboard", icon: "dashboard", end: true },
  { to: "/trades", label: "Trades", icon: "trades" },
  { to: "/positions", label: "Positions", icon: "positions" },
];

const settingsItems = [
  { to: "/settings/broker", label: "Broker", icon: "broker" },
  { to: "/settings/config", label: "Config", icon: "config" },
  { to: "/settings/instance", label: "Instance", icon: "instance" },
];

export default function Layout() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [status, setStatus] = useState("STOPPED");

  // Poll the instance status for the topbar indicator.
  const refreshStatus = useCallback(async () => {
    try {
      const s = await getStatus();
      setStatus(s.status || "STOPPED");
    } catch {
      // Non-fatal — leave the last known status.
    }
  }, []);

  useEffect(() => {
    refreshStatus();
    const id = setInterval(refreshStatus, 10000);
    return () => clearInterval(id);
  }, [refreshStatus]);

  const handleLogout = () => {
    logout();
    navigate("/login", { replace: true });
  };

  const linkClass = ({ isActive }) =>
    `flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors duration-200 ${
      isActive
        ? "bg-emerald-600/15 text-emerald-400"
        : "text-gray-400 hover:bg-gray-700/60 hover:text-gray-100"
    }`;

  const sidebar = (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 px-5 py-5">
        <div className="flex h-8 w-8 items-center justify-center rounded-md bg-emerald-600 font-bold text-white">
          A
        </div>
        <span className="text-lg font-semibold text-gray-100">APEX Trader</span>
      </div>

      <nav className="flex-1 space-y-1 px-3">
        {navItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={linkClass}
            onClick={() => setSidebarOpen(false)}
          >
            <Icon name={item.icon} />
            {item.label}
          </NavLink>
        ))}

        <p className="px-3 pb-1 pt-5 text-xs font-semibold uppercase tracking-wider text-gray-600">
          Settings
        </p>
        {settingsItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={linkClass}
            onClick={() => setSidebarOpen(false)}
          >
            <Icon name={item.icon} />
            {item.label}
          </NavLink>
        ))}
      </nav>

      <div className="border-t border-gray-700 p-4">
        <p className="truncate text-sm text-gray-300" title={user?.email}>
          {user?.email}
        </p>
        <button
          type="button"
          onClick={handleLogout}
          className="mt-3 flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm font-medium text-gray-400 transition-colors duration-200 hover:bg-gray-700/60 hover:text-red-400"
        >
          <Icon name="logout" />
          Logout
        </button>
      </div>
    </div>
  );

  return (
    <div className="flex h-full bg-gray-900 text-gray-100">
      {/* Desktop sidebar */}
      <aside className="hidden w-64 shrink-0 border-r border-gray-700 bg-gray-800 md:block">
        {sidebar}
      </aside>

      {/* Mobile sidebar drawer */}
      {sidebarOpen && (
        <div className="fixed inset-0 z-40 md:hidden">
          <div
            className="absolute inset-0 bg-black/60"
            onClick={() => setSidebarOpen(false)}
          />
          <aside className="absolute left-0 top-0 h-full w-64 border-r border-gray-700 bg-gray-800">
            {sidebar}
          </aside>
        </div>
      )}

      {/* Main column */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-gray-700 bg-gray-800 px-4 py-3">
          <div className="flex items-center gap-3">
            <button
              type="button"
              className="text-gray-400 hover:text-gray-100 md:hidden"
              onClick={() => setSidebarOpen((v) => !v)}
              aria-label="Toggle navigation"
            >
              <Icon name={sidebarOpen ? "close" : "menu"} className="h-6 w-6" />
            </button>
            <span className="text-base font-semibold text-gray-100 md:hidden">
              APEX Trader
            </span>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-xs text-gray-500">Instance</span>
            <StatusBadge status={status} />
          </div>
        </header>

        <main className="apex-scroll flex-1 overflow-y-auto p-4 md:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
