import React, { useState } from 'react';
import { useApi } from '../hooks/useApi';
import ScoreBar from '../components/ScoreBar';

export default function Scanner() {
  const { data } = useApi('/api/scanner', 3000);
  const [catFilter, setCatFilter] = useState('ALL');

  const instruments = data?.instruments || [];
  const filtered = catFilter === 'ALL' ? instruments :
    instruments.filter((i) => i.category === catFilter.toLowerCase());

  return (
    <div>
      <div className="page-header">
        <h2>Scanner</h2>
        <p>
          Live confluence scores — {data?.ready_count || 0} ready,{' '}
          {data?.watchlist_count || 0} on watchlist,{' '}
          {data?.total_count || 0} total instruments
        </p>
      </div>

      <div style={{ marginBottom: '16px' }} className="btn-group">
        {['ALL', 'FOREX', 'COMMODITY', 'INDEX', 'SYNTHETIC'].map((c) => (
          <button key={c} className={`btn ${catFilter === c ? 'btn-accent' : ''}`}
                  onClick={() => setCatFilter(c)}>
            {c}
          </button>
        ))}
      </div>

      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Instrument</th>
                <th>Category</th>
                <th>Direction</th>
                <th style={{ width: '30%' }}>Score</th>
                <th>Status</th>
                <th>Factors</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((inst) => (
                <tr key={inst.symbol}>
                  <td style={{ fontWeight: 600, color: 'var(--text-primary)' }}>
                    {inst.symbol}
                    <div style={{ fontSize: '11px', color: 'var(--text-muted)' }}>{inst.name}</div>
                  </td>
                  <td>
                    <span className="badge badge-blue">{inst.category}</span>
                  </td>
                  <td>
                    <span className={inst.direction === 'LONG' ? 'dir-long' :
                                     inst.direction === 'SHORT' ? 'dir-short' : 'dir-neutral'}
                          style={{ fontWeight: 600 }}>
                      {inst.direction}
                    </span>
                  </td>
                  <td>
                    <ScoreBar score={inst.score} />
                  </td>
                  <td>
                    <span className={`badge ${
                      inst.status === 'READY' ? 'badge-green' :
                      inst.status === 'WATCHLIST' ? 'badge-yellow' : 'badge-red'
                    }`}>
                      {inst.status}
                    </span>
                  </td>
                  <td>
                    {inst.factors && (
                      <div className="factor-grid">
                        {Object.entries(inst.factors).map(([k, v]) => (
                          <div key={k} className="factor-item">
                            <span className="factor-label">{k.replace('_', ' ')}</span>
                            <span className="factor-value" style={{
                              color: v > 10 ? 'var(--green)' : v > 5 ? 'var(--yellow)' : 'var(--text-muted)',
                            }}>{v}</span>
                          </div>
                        ))}
                      </div>
                    )}
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
