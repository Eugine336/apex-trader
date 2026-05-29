import React, { useState } from 'react';
import { useApi } from '../hooks/useApi';

export default function TradeHistory() {
  const { data } = useApi('/api/history', 10000);
  const [filter, setFilter] = useState('ALL');

  const trades = data?.trades || [];
  const filtered = filter === 'ALL' ? trades :
    trades.filter((t) => t.outcome === filter);

  return (
    <div>
      <div className="page-header">
        <h2>Trade History</h2>
        <p>Closed trades with results</p>
      </div>

      <div style={{ marginBottom: '16px' }} className="btn-group">
        {['ALL', 'WIN', 'LOSS'].map((f) => (
          <button key={f} className={`btn ${filter === f ? 'btn-accent' : ''}`}
                  onClick={() => setFilter(f)}>
            {f} {f !== 'ALL' && `(${trades.filter((t) => t.outcome === f).length})`}
          </button>
        ))}
      </div>

      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Instrument</th>
                <th>Direction</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>P&L (pips)</th>
                <th>P&L ($)</th>
                <th>Duration</th>
                <th>Score</th>
                <th>Outcome</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((t) => (
                <tr key={t.id}>
                  <td style={{ fontSize: '12px' }}>{t.opened_at?.slice(0, 10)}</td>
                  <td style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{t.instrument}</td>
                  <td>
                    <span className={t.direction === 'LONG' ? 'dir-long' : 'dir-short'}
                          style={{ fontWeight: 600 }}>
                      {t.direction}
                    </span>
                  </td>
                  <td className="mono">{t.entry_price?.toFixed(5)}</td>
                  <td className="mono">{t.exit_price?.toFixed(5)}</td>
                  <td className={`mono ${t.pnl_pips >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>
                    {t.pnl_pips?.toFixed(1)}
                  </td>
                  <td className={`mono ${t.pnl_dollars >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>
                    ${t.pnl_dollars?.toFixed(2)}
                  </td>
                  <td className="mono">{t.duration_minutes?.toFixed(0)}m</td>
                  <td className="mono" style={{ color: 'var(--accent)' }}>{t.score}</td>
                  <td>
                    <span className={`badge ${t.outcome === 'WIN' ? 'badge-green' : 'badge-red'}`}>
                      {t.outcome}
                    </span>
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
