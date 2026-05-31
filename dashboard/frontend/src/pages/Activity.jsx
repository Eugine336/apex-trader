import React, { useState, useMemo } from 'react';
import { useOutletContext } from 'react-router-dom';

const LEVELS = ['ALL', 'REJECTION', 'WARNING', 'INFO'];

function levelBadgeClass(level) {
  switch ((level || '').toLowerCase()) {
    case 'rejection': return 'badge-red';
    case 'warning':   return 'badge-yellow';
    case 'info':      return 'badge-blue';
    default:          return 'badge-muted';
  }
}

function levelIcon(level) {
  switch ((level || '').toLowerCase()) {
    case 'rejection': return '❌';
    case 'warning':   return '⚠️';
    case 'info':      return 'ℹ️';
    default:          return '•';
  }
}

function formatTime(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString('en-GB', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch {
    return ts;
  }
}

function formatDate(ts) {
  if (!ts) return '';
  try {
    const d = new Date(ts);
    return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' });
  } catch {
    return '';
  }
}

export default function Activity() {
  const { state } = useOutletContext();
  const [levelFilter, setLevelFilter] = useState('ALL');
  const [symbolFilter, setSymbolFilter] = useState('');

  const activity = state.activity || {};
  const events = activity.events || [];

  const filtered = useMemo(() => {
    return events.filter((e) => {
      const matchLevel = levelFilter === 'ALL' || (e.level || '').toUpperCase() === levelFilter;
      const matchSymbol = !symbolFilter || (e.symbol || '').toUpperCase().includes(symbolFilter.toUpperCase());
      return matchLevel && matchSymbol;
    });
  }, [events, levelFilter, symbolFilter]);

  const rejectionCount = events.filter((e) => e.level === 'rejection').length;
  const warningCount   = events.filter((e) => e.level === 'warning').length;

  return (
    <div>
      <div className="page-header">
        <h2>Activity Feed</h2>
        <p>
          {rejectionCount} rejection{rejectionCount !== 1 ? 's' : ''} &nbsp;·&nbsp;
          {warningCount} warning{warningCount !== 1 ? 's' : ''} &nbsp;·&nbsp;
          {events.length} total events
        </p>
      </div>

      {/* Summary stat row */}
      <div className="stat-grid mb-20" style={{ gridTemplateColumns: 'repeat(3, 1fr)' }}>
        <div className="stat-card">
          <div className="stat-label">Rejections</div>
          <div className="stat-value negative">{rejectionCount}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Warnings</div>
          <div className="stat-value" style={{ color: 'var(--yellow-bright)' }}>{warningCount}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Total Events</div>
          <div className="stat-value">{events.length}</div>
        </div>
      </div>

      {/* Filters */}
      <div className="filter-bar" style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 16 }}>
        {LEVELS.map((l) => (
          <button
            key={l}
            className={`filter-btn ${levelFilter === l ? 'active' : ''}`}
            onClick={() => setLevelFilter(l)}
          >
            {l}
          </button>
        ))}
        <input
          type="text"
          placeholder="Filter by symbol…"
          value={symbolFilter}
          onChange={(e) => setSymbolFilter(e.target.value)}
          style={{
            marginLeft: 'auto',
            background: 'var(--bg-card)',
            border: '1px solid var(--border)',
            borderRadius: 6,
            color: 'var(--text-primary)',
            padding: '5px 12px',
            fontSize: 13,
            outline: 'none',
            width: 180,
          }}
        />
      </div>

      {/* Events table */}
      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 90 }}>Time</th>
                <th style={{ width: 60 }}>Date</th>
                <th style={{ width: 100 }}>Level</th>
                <th style={{ width: 100 }}>Symbol</th>
                <th>Message</th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={5} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 48 }}>
                    {events.length === 0
                      ? 'No activity yet — events will appear here as the bot runs.'
                      : 'No events match the current filter.'}
                  </td>
                </tr>
              )}
              {filtered.map((e, i) => (
                <tr key={i}>
                  <td style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--text-muted)' }}>
                    {formatTime(e.timestamp)}
                  </td>
                  <td style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                    {formatDate(e.timestamp)}
                  </td>
                  <td>
                    <span className={`badge ${levelBadgeClass(e.level)}`}>
                      {levelIcon(e.level)} {(e.level || 'info').toUpperCase()}
                    </span>
                  </td>
                  <td style={{ fontWeight: 700, color: 'var(--text-primary)', fontFamily: 'var(--font-mono)' }}>
                    {e.symbol || '—'}
                  </td>
                  <td style={{ color: 'var(--text-secondary)', fontSize: 13 }}>
                    {e.message}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
