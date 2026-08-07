import React from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import StatusBar from './StatusBar';
import ConnectionBanner from './ConnectionBanner';
import useLiveState from '../hooks/useLiveState';

const HOME = { to: '/', icon: '⬡', label: 'Overview' };

const NAV_SECTIONS = [
  {
    section: '① Intelligence',
    items: [
      { to: '/module-votes', icon: '🗳', label: 'Module Votes' },
      { to: '/scanner', icon: '◎', label: 'Scanner' },
    ],
  },
  {
    section: '② Consensus',
    items: [
      { to: '/ranker', icon: '🏆', label: 'Ranker' },
      { to: '/decisions', icon: '🎯', label: 'Decisions' },
      { to: '/decision-trace', icon: '🧬', label: 'Decision Trace' },
      { to: '/orchestrator', icon: '🎛', label: 'Orchestrator' },
    ],
  },
  {
    section: '③ Compliance',
    items: [
      { to: '/risk', icon: '🛡', label: 'Risk Monitor' },
    ],
  },
  {
    section: '④ Portfolio',
    items: [
      { to: '/governor', icon: '🏛', label: 'Governor' },
      { to: '/planner', icon: '🗺', label: 'Planner' },
    ],
  },
  {
    section: '⑤ Execution',
    items: [
      { to: '/operations', icon: '🖥', label: 'Operations' },
    ],
  },
  {
    section: '⑥ Operations',
    items: [
      { to: '/trades', icon: '⚡', label: 'Active Trades' },
      { to: '/position-health', icon: '🩺', label: 'Position Health' },
      { to: '/history', icon: '📋', label: 'Trade History' },
    ],
  },
  {
    section: '⑦ Learning',
    items: [
      { to: '/learning', icon: '🧪', label: 'Learning Layer' },
      { to: '/feedback', icon: '🔁', label: 'Outcome Feedback' },
      { to: '/ml', icon: '🧠', label: 'ML Insights' },
      { to: '/evolution', icon: '🌱', label: 'Evolution (L5)' },
      { to: '/shadow', icon: '👻', label: 'Shadow Outcomes' },
      { to: '/reconciliation', icon: '⚖', label: 'Reconciliation' },
    ],
  },
  {
    section: '⑧ Governance',
    items: [
      { to: '/module-governor', icon: '🚦', label: 'Module Governor' },
    ],
  },
  {
    section: '⑨ Command Center',
    items: [
      { to: '/performance', icon: '📈', label: 'Performance' },
      { to: '/activity', icon: '🔔', label: 'Activity' },
      { to: '/controls', icon: '⚙', label: 'Controls' },
    ],
  },
];

export default function Layout() {
  const { state, connected } = useLiveState();
  const s = state.status || {};

  return (
    <div className="app-layout">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <h1>APEX TRADER</h1>
          <p>Autonomous Trading Organism</p>
        </div>
        <nav className="sidebar-nav">
          <NavLink
            to={HOME.to}
            end
            className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
          >
            <span className="nav-icon">{HOME.icon}</span>
            {HOME.label}
          </NavLink>
          {NAV_SECTIONS.map((sec) => (
            <div key={sec.section}>
              <div className="nav-section-label">{sec.section}</div>
              {sec.items.map((n) => (
                <NavLink
                  key={n.to}
                  to={n.to}
                  className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
                >
                  <span className="nav-icon">{n.icon}</span>
                  {n.label}
                </NavLink>
              ))}
            </div>
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
