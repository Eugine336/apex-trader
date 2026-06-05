import React from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import StatusBar from './StatusBar';
import ConnectionBanner from './ConnectionBanner';
import useLiveState from '../hooks/useLiveState';

const NAV = [
  { to: '/', icon: '⬡', label: 'Overview' },
  { to: '/trades', icon: '⚡', label: 'Active Trades' },
  { to: '/history', icon: '📋', label: 'Trade History' },
  { to: '/scanner', icon: '◎', label: 'Scanner' },
  { to: '/performance', icon: '📈', label: 'Performance' },
  { to: '/risk', icon: '🛡', label: 'Risk Monitor' },
  { to: '/ml', icon: '🧠', label: 'ML Insights' },
  { to: '/activity', icon: '🔔', label: 'Activity' },
  { to: '/shadow', icon: '👻', label: 'Shadow Outcomes' },
  { to: '/reconciliation', icon: '⚖', label: 'Reconciliation' },
  { to: '/controls', icon: '⚙', label: 'Controls' },
];

export default function Layout() {
  const { state, connected } = useLiveState();
  const s = state.status || {};

  return (
    <div className="app-layout">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <h1>APEX TRADER</h1>
          <p>Institutional Trading System</p>
        </div>
        <nav className="sidebar-nav">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.to === '/'}
              className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
            >
              <span className="nav-icon">{n.icon}</span>
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className="conn-pill">
            <span className={`conn-dot ${s.mt5_connected ? 'on' : 'off'}`} />
            MT5
          </div>
          <div className="conn-pill">
            <span className={`conn-dot ${s.deriv_connected ? 'on' : 'off'}`} />
            DERIV
          </div>
        </div>
      </aside>
      <div className="main-wrapper">
        {!connected && <ConnectionBanner />}
        <StatusBar status={s} connected={connected} />
        <main className={`main-content${!connected ? ' disconnected' : ''}`}>
          <Outlet context={{ state, connected }} />
        </main>
      </div>
    </div>
  );
}
