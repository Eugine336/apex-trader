import React, { useState, useMemo } from 'react';
import { useApi } from '../hooks/useApi';

function dirColor(d) {
  const v = (d || '').toUpperCase();
  if (v === 'LONG') return 'var(--green-bright)';
  if (v === 'SHORT') return 'var(--red-bright)';
  return 'var(--text-muted)';
}

function evColor(ev) {
  if (ev > 0.5) return 'var(--green-bright)';
  if (ev < 0) return 'var(--red-bright)';
  return 'var(--yellow-bright)';
}

function OppRow({ o, best }) {
  return (
    <div
      style={{
        borderLeft: `3px solid ${dirColor(o.direction)}`,
        paddingLeft: 10, marginBottom: 6, opacity: best ? 1 : 0.85,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span style={{ fontWeight: 700, fontSize: 12, color: dirColor(o.direction) }}>
          {o.direction} {o.timeframe_class}
        </span>
        {best && <span className="badge badge-green" style={{ fontSize: 9 }}>BEST</span>}
        <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 12, fontWeight: 700, color: evColor(o.expected_value) }}>
          EV {o.expected_value >= 0 ? '+' : ''}{Number(o.expected_value).toFixed(2)}R
        </span>
        <span style={{ fontSize: 10, color: 'var(--text-muted)', marginLeft: 'auto' }}>
          p_win {Math.round(o.win_prob * 100)}% · rr {Number(o.reward_risk).toFixed(1)} · conf {Number(o.confidence).toFixed(2)} · coh {Math.round(o.coherence * 100)}%
        </span>
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 2 }}>
        from: {(o.contributors || []).join(', ') || 'none'}
      </div>
    </div>
  );
}

export default function Ranker() {
  const { data, loading } = useApi('/api/ranker', 5000);
  const [symbolFilter, setSymbolFilter] = useState('');
  const [expanded, setExpanded] = useState(null);

  const pairs = data?.pairs || [];
  const horizon = data?.horizon_distribution || {};
  const dirDist = data?.direction_distribution || {};

  const filtered = useMemo(() => {
    if (!symbolFilter) return pairs;
    const q = symbolFilter.toUpperCase();
    return pairs.filter((p) => (p.pair || '').toUpperCase().includes(q));
  }, [pairs, symbolFilter]);

  return (
    <div>
      <div className="page-header">
        <h2>Opportunity Ranker</h2>
        <p>Coherent vote clusters scored as independent trade ideas — best idea on the board wins, not a summed hand-raise</p>
      </div>

      <div className="stat-grid mb-20" style={{ gridTemplateColumns: 'repeat(5, 1fr)' }}>
        <div className="stat-card">
          <div className="stat-label">Mode</div>
          <div className="stat-value" style={{ color: data?.execute ? 'var(--green-bright)' : 'var(--yellow-bright)', fontSize: 18 }}>
            {data?.execute ? 'LIVE' : 'SHADOW'}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Opportunities</div>
          <div className="stat-value">{data?.total_opportunities || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Avg EV</div>
          <div className="stat-value" style={{ color: evColor(data?.avg_expected_value || 0) }}>
            {(data?.avg_expected_value || 0) >= 0 ? '+' : ''}{Number(data?.avg_expected_value || 0).toFixed(2)}R
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Ranker Overrides</div>
          <div className="stat-value" style={{ color: (data?.override_count || 0) > 0 ? 'var(--accent-bright)' : 'var(--text-primary)' }}>
            {data?.override_count || 0}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">SCALP / SWING</div>
          <div className="stat-value" style={{ fontSize: 18 }}>
            {horizon.SCALP || 0} / {horizon.SWING || 0}
          </div>
        </div>
      </div>

      {/* Distributions */}
      <div className="grid-2 mb-20">
        <div className="card">
          <div className="card-header"><span className="card-title">Horizon Distribution</span></div>
          <div style={{ display: 'flex', gap: 16, padding: '8px 4px' }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>SCALP (fast)</div>
              <div style={{ fontSize: 22, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace" }}>{horizon.SCALP || 0}</div>
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>SWING (slow)</div>
              <div style={{ fontSize: 22, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace" }}>{horizon.SWING || 0}</div>
            </div>
          </div>
        </div>
        <div className="card">
          <div className="card-header"><span className="card-title">Direction Distribution</span></div>
          <div style={{ display: 'flex', gap: 16, padding: '8px 4px' }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 11, color: 'var(--green-bright)' }}>LONG</div>
              <div style={{ fontSize: 22, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", color: 'var(--green-bright)' }}>{dirDist.LONG || 0}</div>
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 11, color: 'var(--red-bright)' }}>SHORT</div>
              <div style={{ fontSize: 22, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", color: 'var(--red-bright)' }}>{dirDist.SHORT || 0}</div>
            </div>
          </div>
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

      {/* Ranked opportunities per pair */}
      <div className="card">
        <div className="card-header"><span className="card-title">Ranked Opportunities — click a row to see every cluster</span></div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 90 }}>Symbol</th>
                <th style={{ width: 130 }}>Best Idea</th>
                <th className="right" style={{ width: 80 }}>EV</th>
                <th style={{ width: 100 }}>Consensus</th>
                <th style={{ width: 90 }}>Selected</th>
                <th className="right" style={{ width: 60 }}>Ideas</th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 && (
                <tr><td colSpan={6} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 48 }}>
                  {loading ? 'Loading…' : 'No ranked opportunities yet — they appear here as the bot scans.'}
                </td></tr>
              )}
              {filtered.map((p) => {
                const isOpen = expanded === p.pair;
                const b = p.best || {};
                return (
                  <React.Fragment key={p.pair}>
                    <tr style={{ cursor: 'pointer' }} onClick={() => setExpanded(isOpen ? null : p.pair)}>
                      <td style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{p.pair}</td>
                      <td style={{ color: dirColor(b.direction), fontWeight: 600 }}>
                        {b.direction} {b.timeframe_class}
                      </td>
                      <td className="right" style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, color: evColor(b.expected_value) }}>
                        {(b.expected_value || 0) >= 0 ? '+' : ''}{Number(b.expected_value || 0).toFixed(2)}R
                      </td>
                      <td style={{ color: dirColor(p.consensus_direction) }}>{p.consensus_direction || '—'}</td>
                      <td>
                        {p.ranker_override
                          ? <span className="badge badge-yellow">OVERRIDE</span>
                          : (p.selected_horizon ? <span className="badge badge-green">{p.selected_horizon}</span> : <span className="badge badge-muted">—</span>)}
                      </td>
                      <td className="right">{p.opportunity_count}</td>
                    </tr>
                    {isOpen && (
                      <tr>
                        <td colSpan={6} style={{ background: 'var(--bg-hover)' }}>
                          <div style={{ padding: '8px 4px' }}>
                            {(p.opportunities || []).map((o, i) => (
                              <OppRow key={i} o={o} best={i === 0} />
                            ))}
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
