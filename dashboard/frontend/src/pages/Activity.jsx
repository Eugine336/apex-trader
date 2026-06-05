import React, { useState, useMemo } from 'react';
import { useOutletContext } from 'react-router-dom';
import { useApi } from '../hooks/useApi';

const LEVELS = ['ALL', 'ERROR', 'WARNING', 'INFO', 'DEBUG'];
const TYPED_EVENTS = ['ALL', 'LOG', 'DECISION_REJECT', 'SETUP_SKIPPED', 'ORDER_SENT', 'ORDER_FILLED', 'TRADE_CLOSE', 'SHADOW_RESOLVED'];

function levelBadgeClass(level) {
  switch ((level || '').toLowerCase()) {
    case 'error':     return 'badge-red';
    case 'warning':   return 'badge-yellow';
    case 'rejection': return 'badge-red';
    case 'debug':     return 'badge-muted';
    case 'info':      return 'badge-blue';
    default:          return 'badge-muted';
  }
}

function levelIcon(level) {
  switch ((level || '').toLowerCase()) {
    case 'error':     return '🔴';
    case 'warning':   return '⚠️';
    case 'rejection': return '❌';
    case 'debug':     return '🔍';
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
  const [typeFilter, setTypeFilter] = useState('ALL');
  const [symbolFilter, setSymbolFilter] = useState('');
  const [showDebug, setShowDebug] = useState(false);

  const sevMin = showDebug ? 'DEBUG' : 'INFO';
  const { data: eventsData } = useApi(`/api/events?severity_min=${sevMin}&limit=300`, 5000);

  const activity = state.activity || {};
  const wsEvents = activity.events || [];
  const apiEvents = (eventsData?.events) || [];
  const events = apiEvents.length > 0 ? apiEvents : wsEvents;

  const filtered = useMemo(() => {
    return events.filter((e) => {
      const lvl = (e.level || e.severity || '').toUpperCase();
      const matchLevel = levelFilter === 'ALL' || lvl === levelFilter;
      const matchType = typeFilter === 'ALL' || (e.event_type || 'LOG') === typeFilter;
      const matchSymbol = !symbolFilter || (e.symbol || '').toUpperCase().includes(symbolFilter.toUpperCase());
      return matchLevel && matchType && matchSymbol;
    });
  }, [events, levelFilter, typeFilter, symbolFilter]);

  const errorCount = events.filter((e) => (e.level || e.severity || '') === 'error' || (e.severity || '') === 'ERROR').length;
  const warningCount = events.filter((e) => (e.level || e.severity || '') === 'warning' || (e.severity || '') === 'WARNING').length;
  const typedCount = events.filter((e) => e.event_type && e.event_type !== 'LOG').length;

  return (
    <div>
      <div className="page-header">
        <h2>Activity Feed</h2>
        <p>
          {errorCount} error{errorCount !== 1 ? 's' : ''} &nbsp;·&nbsp;
          {warningCount} warning{warningCount !== 1 ? 's' : ''} &nbsp;·&nbsp;
          {typedCount} typed event{typedCount !== 1 ? 's' : ''} &nbsp;·&nbsp;
          {events.length} total
        </p>
      </div>

      <div className="stat-grid mb-20" style={{ gridTemplateColumns: 'repeat(4, 1fr)' }}>
        <div className="stat-card">
          <div className="stat-label">Errors</div>
          <div className="stat-value negative">{errorCount}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Warnings</div>
          <div className="stat-value" style={{ color: 'var(--yellow-bright)' }}>{warningCount}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Typed Events</div>
          <div className="stat-value">{typedCount}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Total Events</div>
          <div className="stat-value">{events.length}</div>
        </div>
      </div>

      <div className="filter-bar" style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 16, flexWrap: 'wrap' }}>
        {LEVELS.filter(l => l !== 'DEBUG' || showDebug).map((l) => (
          <button
            key={l}
            className={`filter-btn ${levelFilter === l ? 'active' : ''}`}
            onClick={() => setLevelFilter(l)}
          >
            {l}
          </button>
        ))}
        <span style={{ margin: '0 4px', color: 'var(--text-muted)' }}>|</span>
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          style={{
            background: 'var(--bg-card)', border: '1px solid var(--border)',
            borderRadius: 6, color: 'var(--text-primary)', padding: '5px 8px', fontSize: 13,
          }}
        >
          {TYPED_EVENTS.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
        <label style={{ display: 'flex', alignItems: 'center', gap: 4, marginLeft: 8, fontSize: 13, color: 'var(--text-secondary)', cursor: 'pointer' }}>
          <input type="checkbox" checked={showDebug} onChange={() => setShowDebug(!showDebug)} />
          Show DEBUG
        </label>
        <input
          type="text"
          placeholder="Filter by symbol…"
          value={symbolFilter}
          onChange={(e) => setSymbolFilter(e.target.value)}
          style={{
            marginLeft: 'auto', background: 'var(--bg-card)', border: '1px solid var(--border)',
            borderRadius: 6, color: 'var(--text-primary)', padding: '5px 12px', fontSize: 13, outline: 'none', width: 180,
          }}
        />
      </div>

      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 90 }}>Time</th>
                <th style={{ width: 60 }}>Date</th>
                <th style={{ width: 100 }}>Level</th>
                <th style={{ width: 110 }}>Event Type</th>
                <th style={{ width: 100 }}>Symbol</th>
                <th>Message</th>
                <th style={{ width: 130 }}>Correlation</th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={7} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 48 }}>
                    {events.length === 0
                      ? 'No activity yet — events will appear here as the bot runs.'
                      : 'No events match the current filter.'}
                  </td>
                </tr>
              )}
              {filtered.map((e, i) => (
                <tr key={e.event_id || i}>
                  <td style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--text-muted)' }}>
                    {formatTime(e.timestamp)}
                  </td>
                  <td style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                    {formatDate(e.timestamp)}
                  </td>
                  <td>
                    <span className={`badge ${levelBadgeClass(e.level || e.severity)}`}>
                      {levelIcon(e.level || e.severity)} {(e.level || e.severity || 'info').toUpperCase()}
                    </span>
                  </td>
                  <td style={{ fontSize: 12, color: 'var(--text-secondary)', fontFamily: 'var(--font-mono)' }}>
                    {e.event_type || 'LOG'}
                  </td>
                  <td style={{ fontWeight: 700, color: 'var(--text-primary)', fontFamily: 'var(--font-mono)' }}>
                    {e.symbol || '—'}
                  </td>
                  <td style={{ color: 'var(--text-secondary)', fontSize: 13 }}>
                    {e.message}
                  </td>
                  <td style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-muted)' }}>
                    {e.correlation_id ? e.correlation_id.slice(0, 16) : '—'}
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
