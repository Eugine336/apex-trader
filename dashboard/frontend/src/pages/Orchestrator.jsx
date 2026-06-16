import React, { useState, useMemo } from 'react';
import { useApi } from '../hooks/useApi';

function sizeColor(m) {
  if (m >= 0.9) return 'var(--green-bright)';
  if (m >= 0.7) return 'var(--yellow-bright)';
  if (m <= 0) return 'var(--red-bright)';
  return 'var(--accent-bright)';
}

function DimBar({ d }) {
  const pct = Math.round((d.multiplier ?? d.avg_multiplier ?? 0) * 100);
  const m = d.multiplier ?? d.avg_multiplier ?? 0;
  return (
    <div style={{ marginBottom: 6 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11 }}>
        <span style={{ color: 'var(--text-secondary)' }}>{d.name}</span>
        <span style={{ fontFamily: "'JetBrains Mono', monospace", color: sizeColor(m) }}>×{Number(m).toFixed(2)}</span>
      </div>
      <div style={{ height: 6, background: 'var(--bg-hover)', borderRadius: 3, overflow: 'hidden' }}>
        <div style={{ width: `${pct}%`, height: '100%', background: sizeColor(m) }} />
      </div>
      {d.reason && <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 1 }}>{d.reason}</div>}
    </div>
  );
}

export default function Orchestrator() {
  const { data, loading } = useApi('/api/orchestrator', 5000);
  const [expanded, setExpanded] = useState(null);
  const [symbolFilter, setSymbolFilter] = useState('');

  const proposals = data?.proposals || [];
  const stats = data?.stats || {};
  const cfg = data?.config || {};

  const filtered = useMemo(() => {
    if (!symbolFilter) return proposals;
    const q = symbolFilter.toUpperCase();
    return proposals.filter((p) => (p.pair || '').toUpperCase().includes(q));
  }, [proposals, symbolFilter]);

  return (
    <div>
      <div className="page-header">
        <h2>Orchestrator</h2>
        <p>The round table — every stage's evidence folded into one bounded graded size. Weak dimensions dim the trade; only physics vetoes.</p>
      </div>

      <div className="stat-grid mb-20" style={{ gridTemplateColumns: 'repeat(5, 1fr)' }}>
        <div className="stat-card">
          <div className="stat-label">Mode</div>
          <div className="stat-value" style={{ fontSize: 16, color: cfg.apply_sizing ? 'var(--green-bright)' : 'var(--yellow-bright)' }}>
            {cfg.enabled ? (cfg.apply_sizing ? 'LIVE SIZING' : 'RECORD ONLY') : 'OFF'}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Proposals</div>
          <div className="stat-value">{stats.total || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Avg Size ×</div>
          <div className="stat-value" style={{ color: sizeColor(stats.avg_size_multiplier || 0) }}>
            {Number(stats.avg_size_multiplier || 0).toFixed(2)}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Physics Vetoes</div>
          <div className="stat-value" style={{ color: (stats.vetoed || 0) > 0 ? 'var(--red-bright)' : 'var(--text-primary)' }}>
            {stats.vetoed || 0}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Size Floor</div>
          <div className="stat-value" style={{ fontSize: 18 }}>×{Number(cfg.size_floor || 0).toFixed(2)}</div>
        </div>
      </div>

      <div className="card mb-20">
        <div className="card-header"><span className="card-title">Dimensions dragging size down most (avg multiplier)</span></div>
        <div style={{ padding: '10px 12px' }}>
          {(stats.dimensions || []).length === 0 && (
            <div style={{ color: 'var(--text-muted)', textAlign: 'center', padding: 16 }}>No dimensions yet.</div>
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

      <div className="card">
        <div className="card-header"><span className="card-title">Recent proposals — click a row to see every dimension</span></div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 90 }}>Symbol</th>
                <th style={{ width: 80 }}>Direction</th>
                <th style={{ width: 80 }}>Horizon</th>
                <th className="right" style={{ width: 80 }}>Size ×</th>
                <th style={{ width: 80 }}>Applied</th>
                <th>Verdict</th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 && (
                <tr><td colSpan={6} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 48 }}>
                  {loading ? 'Loading…' : 'No orchestrator proposals yet — they appear here as entries are graded.'}
                </td></tr>
              )}
              {filtered.map((p, idx) => {
                const key = `${p.pair}-${idx}`;
                const isOpen = expanded === key;
                return (
                  <React.Fragment key={key}>
                    <tr style={{ cursor: 'pointer' }} onClick={() => setExpanded(isOpen ? null : key)}>
                      <td style={{ fontWeight: 600 }}>{p.pair}</td>
                      <td style={{ color: p.direction === 'LONG' ? 'var(--green-bright)' : 'var(--red-bright)' }}>{p.direction}</td>
                      <td>{p.horizon || '—'}</td>
                      <td className="right" style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, color: sizeColor(p.size_multiplier) }}>
                        ×{Number(p.size_multiplier || 0).toFixed(2)}
                      </td>
                      <td>{p.applied ? <span className="badge badge-green">YES</span> : <span className="badge badge-muted">no</span>}</td>
                      <td style={{ color: p.vetoed ? 'var(--red-bright)' : 'var(--text-secondary)' }}>
                        {p.vetoed ? `VETO: ${p.veto_reason}` : 'graded'}
                      </td>
                    </tr>
                    {isOpen && (
                      <tr>
                        <td colSpan={6} style={{ background: 'var(--bg-hover)' }}>
                          <div style={{ padding: '10px 12px' }}>
                            {(p.dimensions || []).map((d, i) => <DimBar key={i} d={d} />)}
                            {(p.dimensions || []).length === 0 && (
                              <div style={{ color: 'var(--text-muted)' }}>Physics veto — no dimensions graded.</div>
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
    </div>
  );
}
