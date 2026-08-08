import React, { useState, useMemo } from 'react';
import { useApi } from '../hooks/useApi';

const ACTION_BADGES = {
  HOLD:              'badge-green',
  CLOSE:             'badge-red',
  TIGHTEN_SL:        'badge-yellow',
  MOVE_TO_BREAKEVEN: 'badge-blue',
  TRAIL:             'badge-blue',
  PARTIAL_CLOSE:     'badge-orange',
  SCALE_IN:          'badge-purple',
  ENTER_MARKET:      'badge-green',
  ENTER_PENDING:     'badge-green',
  SKIP:              'badge-muted',
  OBSERVE:           'badge-muted',
  SET_PROTECTIVE_STOP: 'badge-yellow',
};

function actionBadge(action) {
  return ACTION_BADGES[(action || '').toUpperCase()] || 'badge-muted';
}

function fmtTs(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts);
    return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) + ' ' +
           d.toLocaleTimeString('en-GB', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch {
    return '—';
  }
}

function fmtDim(v) {
  if (v == null) return '—';
  const n = Number(v);
  return (n >= 0 ? '+' : '') + n.toFixed(3);
}

function dimColor(val, min, max) {
  if (val == null) return 'var(--text-muted)';
  const n = Number(val);
  if (min < 0) {
    if (n > 0.3) return 'var(--green-bright)';
    if (n < -0.3) return 'var(--red-bright)';
    return 'var(--yellow-bright)';
  }
  if (n > 0.7) return 'var(--green-bright)';
  if (n < 0.4) return 'var(--red-bright)';
  return 'var(--yellow-bright)';
}

function dimBarWidth(val, min, max) {
  if (val == null) return 0;
  const n = Number(val);
  return Math.max(0, Math.min(100, ((n - min) / (max - min)) * 100));
}

function truncate(s, len) {
  if (!s) return '—';
  return s.length > len ? s.slice(0, len) + '…' : s;
}

const TYPE_FILTERS = ['ALL', 'MANAGEMENT', 'ENTRY'];

export default function Decisions() {
  const { data, loading } = useApi('/api/decisions?limit=100', 5000);
  const [typeFilter, setTypeFilter] = useState('ALL');
  const [symbolFilter, setSymbolFilter] = useState('');

  const decisions = data?.decisions || [];
  const stats = data?.stats || {};
  const dims = stats.dimensions || {};
  const actionCounts = stats.action_counts || {};
  const sitCounts = stats.situation_counts || {};

  const filtered = useMemo(() => {
    return decisions.filter(d => {
      const dt = (d.decision_type || 'MANAGEMENT').toUpperCase();
      const matchType = typeFilter === 'ALL' || dt === typeFilter;
      const matchSym = !symbolFilter || (d.symbol || '').toUpperCase().includes(symbolFilter.toUpperCase());
      return matchType && matchSym;
    });
  }, [decisions, typeFilter, symbolFilter]);

  const actionEntries = Object.entries(actionCounts).sort((a, b) => b[1] - a[1]);
  const sitEntries = Object.entries(sitCounts).sort((a, b) => b[1] - a[1]);

  return (
    <div>
      <div className="page-header">
        <h2>Decision Intelligence</h2>
        <p>Situation assessments, contextual decisions, and governor oversight</p>
      </div>

      {/* Stats cards */}
      <div className="stat-grid mb-20" style={{ gridTemplateColumns: 'repeat(4, 1fr)' }}>
        <div className="stat-card">
          <div className="stat-label">Total Decisions</div>
          <div className="stat-value">{stats.total_decisions || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Governor Vetoes</div>
          <div className="stat-value" style={{ color: (stats.governor_vetoes || 0) > 0 ? 'var(--red-bright)' : 'var(--text-primary)' }}>
            {stats.governor_vetoes || 0}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Governor Changes</div>
          <div className="stat-value" style={{ color: (stats.governor_changes || 0) > 0 ? 'var(--yellow-bright)' : 'var(--text-primary)' }}>
            {stats.governor_changes || 0}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Unique Actions</div>
          <div className="stat-value">{actionEntries.length}</div>
        </div>
      </div>

      {/* Situation Dimensions */}
      {Object.keys(dims).length > 0 && (
        <div className="card mb-20">
          <div className="card-header"><span className="card-title">Average Situation Dimensions</span></div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 16 }}>
            {[
              { key: 'avg_tf_alignment', label: 'TF Alignment', min: -1, max: 1 },
              { key: 'avg_momentum', label: 'Momentum', min: -1, max: 1 },
              { key: 'avg_structure_integrity', label: 'Structure', min: 0, max: 1 },
              { key: 'avg_read_confidence', label: 'Confidence', min: 0, max: 1 },
            ].map(d => {
              const v = dims[d.key];
              return (
                <div key={d.key} style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: 10, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', fontWeight: 600, marginBottom: 8 }}>
                    {d.label}
                  </div>
                  <div className="gauge-bar-outer">
                    <div
                      className="gauge-bar-fill"
                      style={{
                        width: `${dimBarWidth(v, d.min, d.max)}%`,
                        background: dimColor(v, d.min, d.max),
                      }}
                    />
                  </div>
                  <div style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 600, fontSize: 18, color: dimColor(v, d.min, d.max), marginTop: 6 }}>
                    {fmtDim(v)}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Action Distribution + Situation Labels — side by side */}
      <div className="grid-2 mb-20">
        {/* Action Distribution */}
        <div className="card">
          <div className="card-header"><span className="card-title">Action Distribution</span></div>
          {actionEntries.length === 0 ? (
            <div style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24, fontSize: 12 }}>No decisions recorded yet.</div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {actionEntries.map(([action, count]) => {
                const total = stats.total_decisions || 1;
                const pct = ((count / total) * 100).toFixed(0);
                return (
                  <div key={action} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span className={`badge ${actionBadge(action)}`} style={{ minWidth: 120, textAlign: 'center' }}>{action}</span>
                    <div style={{ flex: 1, height: 6, background: 'var(--bg-hover)', borderRadius: 3, overflow: 'hidden' }}>
                      <div style={{ height: '100%', width: `${pct}%`, borderRadius: 3, background: 'var(--accent-bright)', transition: 'width 0.3s' }} />
                    </div>
                    <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 12, fontWeight: 600, minWidth: 50, textAlign: 'right', color: 'var(--text-secondary)' }}>
                      {count} ({pct}%)
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Situation Labels */}
        <div className="card">
          <div className="card-header"><span className="card-title">Situation Labels</span></div>
          {sitEntries.length === 0 ? (
            <div style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24, fontSize: 12 }}>No situations classified yet.</div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {sitEntries.map(([label, count]) => {
                const total = stats.total_decisions || 1;
                const pct = ((count / total) * 100).toFixed(0);
                return (
                  <div key={label} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, fontWeight: 600, color: 'var(--text-secondary)', minWidth: 160 }}>
                      {label}
                    </span>
                    <div style={{ flex: 1, height: 6, background: 'var(--bg-hover)', borderRadius: 3, overflow: 'hidden' }}>
                      <div style={{ height: '100%', width: `${pct}%`, borderRadius: 3, background: 'var(--purple)', transition: 'width 0.3s' }} />
                    </div>
                    <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 12, fontWeight: 600, minWidth: 50, textAlign: 'right', color: 'var(--text-secondary)' }}>
                      {count} ({pct}%)
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {/* Filters */}
      <div className="filter-bar" style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 16, flexWrap: 'wrap' }}>
        {TYPE_FILTERS.map(t => (
          <button key={t} className={`filter-btn ${typeFilter === t ? 'active' : ''}`} onClick={() => setTypeFilter(t)}>{t}</button>
        ))}
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

      {/* Recent Decisions Table */}
      <div className="card">
        <div className="card-header"><span className="card-title">Recent Decisions</span></div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 130 }}>Time</th>
                <th style={{ width: 90 }}>Symbol</th>
                <th style={{ width: 55 }}>Dir</th>
                <th style={{ width: 55 }}>Type</th>
                <th style={{ width: 120 }}>Action</th>
                <th style={{ width: 140 }}>Situation</th>
                <th className="right" style={{ width: 60 }}>Align</th>
                <th className="right" style={{ width: 55 }}>Score</th>
                <th className="right" style={{ width: 60 }}>PnL</th>
                <th style={{ width: 40 }}>Gov</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 && (
                <tr><td colSpan={11} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 48 }}>
                  {loading ? 'Loading…' : 'No decisions recorded yet — decisions will appear here as the bot runs.'}
                </td></tr>
              )}
              {filtered.map((d, i) => {
                const dec = d.decision || {};
                const sit = d.situation || {};
                const dir = d.direction || '';
                const dt = (d.decision_type || 'MGMT').slice(0, 4).toUpperCase();
                const pnlPips = d.pnl_pips;
                const govChanged = dec.governor_changed || dec.governor_vetoed;
                return (
                  <tr key={d.order_id ? `${d.order_id}-${i}` : i}>
                    <td style={{ fontSize: 11, color: 'var(--text-muted)' }}>{fmtTs(d.timestamp)}</td>
                    <td style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{d.symbol || '—'}</td>
                    <td className={dir === 'BUY' || dir === 'LONG' ? 'dir-long' : dir === 'SELL' || dir === 'SHORT' ? 'dir-short' : 'dir-neutral'}>
                      {dir || '—'}
                    </td>
                    <td><span className={`badge ${dt === 'ENTR' ? 'badge-blue' : 'badge-muted'}`}>{dt}</span></td>
                    <td><span className={`badge ${actionBadge(dec.action)}`}>{dec.action || '—'}</span></td>
                    <td style={{ fontSize: 11, color: 'var(--text-secondary)' }}>{sit.label || '—'}</td>
                    <td className="right" style={{ color: dimColor(sit.tf_alignment, -1, 1) }}>
                      {sit.tf_alignment != null ? fmtDim(sit.tf_alignment) : '—'}
                    </td>
                    <td className="right">{d.scan_score != null ? d.scan_score : '—'}</td>
                    <td className="right" style={{ color: pnlPips > 0 ? 'var(--green-bright)' : pnlPips < 0 ? 'var(--red-bright)' : 'var(--text-secondary)' }}>
                      {pnlPips != null ? (pnlPips >= 0 ? '+' : '') + Number(pnlPips).toFixed(1) : '—'}
                    </td>
                    <td style={{ textAlign: 'center' }}>{govChanged ? '⚠️' : '—'}</td>
                    <td className="text-col" style={{ fontSize: 11, color: 'var(--text-muted)' }}>{truncate(dec.reason, 60)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
