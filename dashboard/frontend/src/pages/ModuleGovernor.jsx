import React, { useMemo } from 'react';
import { useApi } from '../hooks/useApi';

function modeStyle(mode) {
  const m = (mode || 'ACTIVE').toUpperCase();
  if (m === 'SHADOW') {
    return { color: 'var(--amber, #f59e0b)', background: 'rgba(245,158,11,0.15)' };
  }
  if (m === 'DISABLED') {
    return { color: 'var(--red-bright)', background: 'rgba(239,68,68,0.15)' };
  }
  return { color: 'var(--green-bright)', background: 'rgba(34,197,94,0.12)' };
}

function fmtAge(seconds) {
  const s = Number(seconds || 0);
  if (s <= 0) return '—';
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

function fmtTime(ts) {
  const t = Number(ts || 0);
  if (!t) return '—';
  try {
    return new Date(t * 1000).toLocaleTimeString();
  } catch {
    return '—';
  }
}

export default function ModuleGovernor() {
  const { data, loading } = useApi('/api/module-governor', 5000);

  const modules = data?.modules || [];
  const transitions = data?.transitions || [];
  const counts = data?.counts || {};
  const enabled = !!data?.enabled;

  const sorted = useMemo(
    () =>
      [...modules].sort((a, b) => {
        const order = { SHADOW: 0, DISABLED: 1, ACTIVE: 2 };
        const am = order[(a.mode || 'ACTIVE').toUpperCase()] ?? 3;
        const bm = order[(b.mode || 'ACTIVE').toUpperCase()] ?? 3;
        if (am !== bm) return am - bm;
        return (a.module || '').localeCompare(b.module || '');
      }),
    [modules],
  );

  return (
    <div>
      <div className="page-header">
        <h2>Module Governor</h2>
        <p>
          Shadow mode — a module whose graded accuracy drops keeps running and being
          measured, but its vote is suppressed (weight 0). It returns to ACTIVE if it
          recovers, or is DISABLED if it stays poor.
        </p>
      </div>

      {!enabled && (
        <div
          className="card mb-20"
          style={{ borderLeft: '3px solid var(--amber, #f59e0b)', padding: 12, fontSize: 13 }}
        >
          Module governor is <strong>disabled</strong> — every module votes normally.
          Enable <code>module_governor.module_governor_enabled</code> to activate shadow mode.
        </div>
      )}

      <div className="stat-grid mb-20" style={{ gridTemplateColumns: 'repeat(4, 1fr)' }}>
        <div className="stat-card">
          <div className="stat-label">Modules</div>
          <div className="stat-value">{data?.module_count || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Active</div>
          <div className="stat-value" style={{ color: 'var(--green-bright)' }}>
            {counts.ACTIVE || 0}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Shadow</div>
          <div className="stat-value" style={{ color: 'var(--amber, #f59e0b)' }}>
            {counts.SHADOW || 0}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Disabled</div>
          <div className="stat-value" style={{ color: 'var(--red-bright)' }}>
            {counts.DISABLED || 0}
          </div>
        </div>
      </div>

      {/* Per-module status */}
      <div className="card mb-20">
        <div className="card-header">
          <span className="card-title">Module Status</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Module</th>
                <th style={{ width: 100 }}>Mode</th>
                <th className="right" style={{ width: 90 }}>Acc @ Move</th>
                <th className="right" style={{ width: 90 }}>In Mode</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {sorted.length === 0 && (
                <tr>
                  <td colSpan={5} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 32 }}>
                    {loading ? 'Loading…' : 'No governed modules yet.'}
                  </td>
                </tr>
              )}
              {sorted.map((m) => (
                <tr key={m.module}>
                  <td style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{m.module}</td>
                  <td>
                    <span
                      className="badge"
                      style={{ ...modeStyle(m.mode), fontWeight: 700, padding: '2px 8px', borderRadius: 4 }}
                    >
                      {(m.mode || 'ACTIVE').toUpperCase()}
                    </span>
                  </td>
                  <td className="right" style={{ fontFamily: "'JetBrains Mono', monospace" }}>
                    {Number(m.accuracy_at_transition || 0).toFixed(2)}
                  </td>
                  <td className="right" style={{ color: 'var(--text-muted)' }}>
                    {fmtAge(m.seconds_in_mode)}
                  </td>
                  <td style={{ color: 'var(--text-muted)', fontSize: 12 }}>{m.reason || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Transition history */}
      <div className="card">
        <div className="card-header">
          <span className="card-title">Transition History</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 110 }}>Time</th>
                <th>Module</th>
                <th style={{ width: 160 }}>Change</th>
                <th className="right" style={{ width: 80 }}>Acc</th>
                <th className="right" style={{ width: 70 }}>N</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {transitions.length === 0 && (
                <tr>
                  <td colSpan={6} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 32 }}>
                    {loading ? 'Loading…' : 'No transitions recorded yet.'}
                  </td>
                </tr>
              )}
              {transitions.map((t, i) => (
                <tr key={`${t.module}-${t.timestamp}-${i}`}>
                  <td style={{ color: 'var(--text-muted)', fontSize: 12 }}>{fmtTime(t.timestamp)}</td>
                  <td style={{ fontWeight: 600 }}>{t.module}</td>
                  <td style={{ fontSize: 12 }}>
                    <span style={modeStyle(t.old_mode)}>{(t.old_mode || '').toUpperCase()}</span>
                    {' → '}
                    <span style={modeStyle(t.new_mode)}>{(t.new_mode || '').toUpperCase()}</span>
                  </td>
                  <td className="right" style={{ fontFamily: "'JetBrains Mono', monospace" }}>
                    {Number(t.accuracy || 0).toFixed(2)}
                  </td>
                  <td className="right" style={{ color: 'var(--text-muted)' }}>{t.sample_size || 0}</td>
                  <td style={{ color: 'var(--text-muted)', fontSize: 12 }}>{t.reason || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
