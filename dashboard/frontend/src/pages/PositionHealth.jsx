import React, { useState, useMemo } from 'react';
import { useApi } from '../hooks/useApi';

// Health → colour: green healthy, yellow tighten, orange trim, red exit.
function healthColor(h) {
  if (h >= 0.8) return 'var(--green-bright)';
  if (h >= 0.6) return 'var(--yellow-bright)';
  if (h >= 0.4) return 'var(--accent-bright)';
  if (h >= 0.2) return 'var(--orange-bright, #f59e0b)';
  return 'var(--red-bright)';
}

function actionBadge(action) {
  const map = {
    HOLD: 'badge-green',
    TIGHTEN_SL: 'badge-muted',
    SCALE_DOWN: 'badge-muted',
    EXIT_PARTIAL: 'badge-muted',
    EXIT_FULL: 'badge-red',
    SCALE_UP: 'badge-green',
  };
  return map[action] || 'badge-muted';
}

function DimBar({ d }) {
  const m = d.multiplier ?? d.avg_multiplier ?? 0;
  const pct = Math.round(m * 100);
  return (
    <div style={{ marginBottom: 6 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11 }}>
        <span style={{ color: 'var(--text-secondary)' }}>{d.name}</span>
        <span style={{ fontFamily: "'JetBrains Mono', monospace", color: healthColor(m) }}>×{Number(m).toFixed(2)}</span>
      </div>
      <div style={{ height: 6, background: 'var(--bg-hover)', borderRadius: 3, overflow: 'hidden' }}>
        <div style={{ width: `${pct}%`, height: '100%', background: healthColor(m) }} />
      </div>
      {d.reason && <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 1 }}>{d.reason}</div>}
    </div>
  );
}

// Compact health-over-time sparkline (no chart lib dependency).
function Spark({ series }) {
  const pts = (series || []).map((s) => s.health_score ?? 0);
  if (pts.length === 0) return <span style={{ color: 'var(--text-muted)' }}>—</span>;
  const w = 120, h = 22;
  const step = pts.length > 1 ? w / (pts.length - 1) : w;
  const path = pts
    .map((v, i) => `${i === 0 ? 'M' : 'L'} ${(i * step).toFixed(1)} ${(h - v * h).toFixed(1)}`)
    .join(' ');
  const last = pts[pts.length - 1];
  return (
    <svg width={w} height={h} style={{ display: 'block' }}>
      <line x1="0" y1={h - 0.8 * h} x2={w} y2={h - 0.8 * h} stroke="var(--border)" strokeDasharray="2 2" />
      <path d={path} fill="none" stroke={healthColor(last)} strokeWidth="1.5" />
    </svg>
  );
}

export default function PositionHealth() {
  const { data, loading } = useApi('/api/position-health', 5000);
  const [expanded, setExpanded] = useState(null);
  const [symbolFilter, setSymbolFilter] = useState('');

  const positions = data?.positions || [];
  const reports = data?.reports || [];
  const stats = data?.stats || {};
  const cfg = data?.config || {};

  const filteredPos = useMemo(() => {
    if (!symbolFilter) return positions;
    const q = symbolFilter.toUpperCase();
    return positions.filter((p) => (p.pair || '').toUpperCase().includes(q));
  }, [positions, symbolFilter]);

  const actionLog = useMemo(() => {
    let rows = reports.filter((r) => r.action && r.action !== 'HOLD');
    if (symbolFilter) {
      const q = symbolFilter.toUpperCase();
      rows = rows.filter((r) => (r.pair || '').toUpperCase().includes(q));
    }
    return rows.slice(-50).reverse();
  }, [reports, symbolFilter]);

  return (
    <div>
      <div className="page-header">
        <h2>Position Health</h2>
        <p>The round table for OPEN trades — every cycle each position's evidence is regraded into a continuous health score and a bounded management action. A one-way de-risk: hold, tighten, trim or exit.</p>
      </div>

      <div className="stat-grid mb-20" style={{ gridTemplateColumns: 'repeat(4, 1fr)' }}>
        <div className="stat-card">
          <div className="stat-label">Mode</div>
          <div className="stat-value" style={{ fontSize: 16, color: cfg.manage_open_positions ? 'var(--green-bright)' : 'var(--yellow-bright)' }}>
            {cfg.manage_open_positions ? 'LIVE MANAGE' : 'LEGACY'}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Open Positions</div>
          <div className="stat-value">{positions.length}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Avg Health</div>
          <div className="stat-value" style={{ color: healthColor(stats.avg_health || 0) }}>
            {Number(stats.avg_health || 0).toFixed(2)}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Scale-Up</div>
          <div className="stat-value" style={{ fontSize: 16 }}>{cfg.allow_scale_up ? 'ON' : 'OFF'}</div>
        </div>
      </div>

      <div className="card mb-20">
        <div className="card-header"><span className="card-title">Dimensions dragging health down most (avg multiplier)</span></div>
        <div style={{ padding: '10px 12px' }}>
          {(stats.dimensions || []).length === 0 && (
            <div style={{ color: 'var(--text-muted)', textAlign: 'center', padding: 16 }}>No evaluations yet.</div>
          )}
          {(stats.dimensions || []).map((d) => <DimBar key={d.name} d={d} />)}
        </div>
      </div>

      <div className="filter-bar" style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 16 }}>
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

      <div className="card mb-20">
        <div className="card-header"><span className="card-title">Open positions — click a row to see the dimension breakdown + entry vs now</span></div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 90 }}>Symbol</th>
                <th style={{ width: 70 }}>Dir</th>
                <th style={{ width: 70 }}>Horizon</th>
                <th className="right" style={{ width: 70 }}>Health</th>
                <th style={{ width: 110 }}>Action</th>
                <th className="right" style={{ width: 70 }}>P&amp;L (R)</th>
                <th style={{ width: 130 }}>Trend</th>
              </tr>
            </thead>
            <tbody>
              {filteredPos.length === 0 && (
                <tr><td colSpan={7} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 48 }}>
                  {loading ? 'Loading…' : 'No open-position health evaluations yet — they appear as positions are managed.'}
                </td></tr>
              )}
              {filteredPos.map((p, idx) => {
                const key = `${p.order_id || p.pair}-${idx}`;
                const isOpen = expanded === key;
                const latest = (reports || []).filter((r) => String(r.order_id || r.pair) === String(p.order_id || p.pair)).slice(-1)[0] || {};
                return (
                  <React.Fragment key={key}>
                    <tr style={{ cursor: 'pointer' }} onClick={() => setExpanded(isOpen ? null : key)}>
                      <td style={{ fontWeight: 600 }}>{p.pair}</td>
                      <td style={{ color: (p.direction || '').toUpperCase().startsWith('B') || p.direction === 'LONG' ? 'var(--green-bright)' : 'var(--red-bright)' }}>{p.direction}</td>
                      <td>{p.horizon || '—'}</td>
                      <td className="right" style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, color: healthColor(p.health_score) }}>
                        {Number(p.health_score || 0).toFixed(2)}
                      </td>
                      <td><span className={`badge ${actionBadge(p.action)}`}>{p.action || '—'}</span></td>
                      <td className="right" style={{ fontFamily: "'JetBrains Mono', monospace", color: (p.profit_r || 0) >= 0 ? 'var(--green-bright)' : 'var(--red-bright)' }}>
                        {Number(p.profit_r || 0).toFixed(2)}
                      </td>
                      <td><Spark series={p.series} /></td>
                    </tr>
                    {isOpen && (
                      <tr>
                        <td colSpan={7} style={{ background: 'var(--bg-hover)' }}>
                          <div style={{ padding: '10px 12px' }}>
                            {latest.entry_health_at_open != null && (
                              <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 8 }}>
                                Entry health <b>{Number(latest.entry_health_at_open).toFixed(2)}</b> → now <b style={{ color: healthColor(latest.health_score) }}>{Number(latest.health_score || 0).toFixed(2)}</b>
                                {' '}(Δ {Number(latest.health_delta || 0).toFixed(2)})
                              </div>
                            )}
                            {(latest.dimensions || []).map((d, i) => <DimBar key={i} d={d} />)}
                            {(latest.thesis_changes || []).length > 0 && (
                              <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 6 }}>
                                Changes since entry: {(latest.thesis_changes || []).join('; ')}
                              </div>
                            )}
                            {(latest.dimensions || []).length === 0 && (
                              <div style={{ color: 'var(--text-muted)' }}>No dimension detail recorded.</div>
                            )}
                          </div>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <div className="card-header"><span className="card-title">Management action log (non-HOLD)</span></div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 160 }}>Time</th>
                <th style={{ width: 90 }}>Symbol</th>
                <th className="right" style={{ width: 70 }}>Health</th>
                <th style={{ width: 120 }}>Action</th>
                <th>Changes</th>
              </tr>
            </thead>
            <tbody>
              {actionLog.length === 0 && (
                <tr><td colSpan={5} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 32 }}>
                  No management actions yet — healthy positions HOLD.
                </td></tr>
              )}
              {actionLog.map((r, i) => (
                <tr key={i}>
                  <td style={{ fontSize: 11, color: 'var(--text-muted)' }}>{(r._event_ts || '').replace('T', ' ').slice(0, 19)}</td>
                  <td style={{ fontWeight: 600 }}>{r.pair}</td>
                  <td className="right" style={{ fontFamily: "'JetBrains Mono', monospace", color: healthColor(r.health_score) }}>{Number(r.health_score || 0).toFixed(2)}</td>
                  <td><span className={`badge ${actionBadge(r.action)}`}>{r.action}</span></td>
                  <td style={{ fontSize: 11, color: 'var(--text-secondary)' }}>{(r.thesis_changes || []).join('; ') || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
