import React from 'react';
import { NavLink } from 'react-router-dom';
import StatusBar from './StatusBar';

const NAV = [
  { to: '/', label: 'Overview', icon: '◉' },
  { to: '/trades', label: 'Active Trades', icon: '⚡' },
  { to: '/history', label: 'Trade History', icon: '📋' },
  { to: '/scanner', label: 'Scanner', icon: '🔍' },
  { to: '/performance', label: 'Performance', icon: '📈' },
  { to: '/risk', label: 'Risk Monitor', icon: '🛡' },
  { to: '/ml', label: 'ML Insights', icon: '🧠' },
  { to: '/controls', label: 'Controls', icon: '⚙' },
];

export default function Layout({ children }) {
  return (
    <div className="app-layout">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <h1>APEX TRADER</h1>
          <p>Sharp. Precise. Always watching.</p>
        </div>
        <nav className="sidebar-nav">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              className={({ isActive }) =>
                `nav-item${isActive ? ' active' : ''}`
              }
            >
              <span>{item.icon}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main className="main-content">
        <StatusBar />
        {children}
      </main>
    </div>
  );
}
