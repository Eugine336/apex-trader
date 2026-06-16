import React, { useState, useMemo } from 'react';
import { useApi } from '../hooks/useApi';

function dirColor(d) {
  const v = (d || '').toUpperCase();
  if (v === 'LONG') return 'var(--green-bright)';
  if (v === 'SHORT') return 'var(--red-bright)';
  return 'var(--text-muted)';
}

function dirCell(d) {
  const v = (d || 'NEUTRAL').toUpperCase();
  const bg =
    v === 'LONG' ? 'rgba(34,197,94,0.15)' : v === 'SHORT' ? 'rgba(239,68,68,0.15)' : 'transparent';
  return { color: dirColor(v), background: bg };
}

export default function ModuleVotes() {
  const { data, loading } = useApi('/api/module-votes', 5000);
  const [symbolFilter, setSymbolFilter] = useState('');

  const pairs = data?.pairs || [];
  const modules = data?.modules || [];

  // Stable column order = modules sorted by participation (from backend).
  const moduleNames = useMemo(() => modules.map((m) => m.module), [modules]);

  const filtered = useMemo(() => {
    if (!symbolFilter) return pairs;
    const q = symbolFilter.toUpperCase();
    return pairs.filter((p) => (p.pair || '').toUpperCase().includes(q));
  }, [pairs, symbolFilter]);

  const voteByModule = (p) => {
    const m = {};
    (p.votes || []).forEach((v) => {
      m[v.module] = v;
    });
    return m;
  };

  return (
    <div>
      <div className="page-header">
        <h2>Module Votes</h2>
        <p>What each of the brain's analysis modules saw this scan — the raw evidence before the ranker clusters it</p>
      </div>

      <div className="stat-grid mb-20" style={{ gridTemplateColumns: 'repeat(4, 1fr)' }}>
        <div className="stat-card">
          <div className="stat-label">Pairs Voting</div>
          <div className="stat-value">{data?.pair_count || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Active Modules</div>
          <div className="stat-value">{modules.length}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">LONG Leans</div>
          <div className="stat-value" style={{ color: 'var(--green-bright)' }}>
            {pairs.filter((p) => (p.net_signed || 0) > 0).length}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">SHORT Leans</div>
          <div className="stat-value" style={{ color: 'var(--red-bright)' }}>
            {pairs.filter((p) => (p.net_signed || 0) < 0).length}
          </div>
        </div>
      </div>

      {/* Per-module participation summary */}
      <div className="card mb-20">
        <div className="card-header"><span className="card-title">Module Participation — across all pairs this scan</span></div>
        {modules.length === 0 ? (
          <div style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24, fontSize: 12 }}>
            {loading ? 'Loading…' : 'No module votes yet — they appear here as the bot scans.'}
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Module</th>
                  <th style={{ width: 80 }}>Horizon</th>
                  <th className="right" style={{ width: 70 }}>LONG</th>
                  <th className="right" style={{ width: 70 }}>SHORT</th>
                  <th className="right" style={{ width: 80 }}>NEUTRAL</th>
                  <th className="right" style={{ width: 90 }}>Avg Conf</th>
                </tr>
              </thead>
              <tbody>
                {modules.map((m) => (
                  <tr key={m.module}>
                    <td style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{m.module}</td>
                    <td>
                      <span className="badge badge-muted">{m.horizon}</span>
                    </td>
                    <td className="right" style={{ color: 'var(--green-bright)' }}>{m.long}</td>
                    <td className="right" style={{ color: 'var(--red-bright)' }}>{m.short}</td>
                    <td className="right" style={{ color: 'var(--text-muted)' }}>{m.neutral}</td>
                    <td className="right" style={{ fontFamily: "'JetBrains Mono', monospace" }}>
                      {Number(m.avg_confidence || 0).toFixed(2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
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

      {/* Vote grid: pairs (rows) × modules (cols) */}
      <div className="card">
        <div className="card-header"><span className="card-title">Vote Grid — green LONG · red SHORT · grey NEUTRAL</span></div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 90 }}>Symbol</th>
                <th style={{ width: 90 }}>Consensus</th>
                <th className="right" style={{ width: 70 }}>Net</th>
                {moduleNames.map((m) => (
                  <th key={m} style={{ fontSize: 10, textAlign: 'center' }}>{m}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={3 + moduleNames.length} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 48 }}>
                    {loading ? 'Loading…' : 'No votes recorded yet.'}
                  </td>
                </tr>
              )}
              {filtered.map((p) => {
                const vm = voteByModule(p);
                return (
                  <tr key={p.pair}>
                    <td style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{p.pair}</td>
                    <td style={{ color: dirColor(p.consensus_direction || p.direction) }}>
                      {p.consensus_direction || p.direction || '—'}
                    </td>
                    <td className="right" style={{ fontFamily: "'JetBrains Mono', monospace", color: dirColor(p.net_signed > 0 ? 'LONG' : p.net_signed < 0 ? 'SHORT' : '') }}>
                      {Number(p.net_signed || 0).toFixed(2)}
                    </td>
                    {moduleNames.map((m) => {
                      const v = vm[m];
                      if (!v) {
                        return <td key={m} style={{ textAlign: 'center', color: 'var(--text-muted)' }}>·</td>;
                      }
                      return (
                        <td
                          key={m}
                          title={`${m}: ${v.direction} conf ${Number(v.confidence).toFixed(2)} (${v.horizon})`}
                          style={{
                            textAlign: 'center', fontSize: 10, fontWeight: 700,
                            ...dirCell(v.direction),
                          }}
                        >
                          {v.direction === 'LONG' ? 'L' : v.direction === 'SHORT' ? 'S' : '·'}
                          <div style={{ fontSize: 9, fontWeight: 400, opacity: 0.8 }}>
                            {v.direction !== 'NEUTRAL' ? Number(v.confidence).toFixed(2) : ''}
                          </div>
                        </td>
                      );
                    })}
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
