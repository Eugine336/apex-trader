import React from 'react';
import { useApi } from '../hooks/useApi';

export default function ActiveTrades() {
  const { data } = useApi('/api/trades', 2000);

  return (
    <div>
      <div className="page-header">
        <h2>Active Trades</h2>
        <p>All open positions with real-time P&L</p>
      </div>

      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Instrument</th>
                <th>Direction</th>
                <th>Entry</th>
                <th>Current</th>
                <th>SL</th>
                <th>TP1</th>
                <th>TP2</th>
                <th>P&L (pips)</th>
                <th>P&L ($)</th>
                <th>Lots</th>
                <th>Score</th>
                <th>Stage</th>
              </tr>
            </thead>
            <tbody>
              {data?.trades?.map((t) => (
                <tr key={t.id}>
                  <td style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{t.instrument}</td>
                  <td>
                    <span className={t.direction === 'LONG' ? 'dir-long' : 'dir-short'}
                          style={{ fontWeight: 600 }}>
                      {t.direction}
                    </span>
                  </td>
                  <td className="mono">{t.entry_price}</td>
                  <td className="mono">{t.current_price}</td>
                  <td className="mono" style={{ color: 'var(--red)' }}>{t.stop_loss}</td>
                  <td className="mono">{t.tp1}</td>
                  <td className="mono">{t.tp2}</td>
                  <td className={`mono ${t.pnl_pips >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>
                    {t.pnl_pips?.toFixed(1)}
                  </td>
                  <td className={`mono ${t.pnl_dollars >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>
                    ${t.pnl_dollars?.toFixed(2)}
                  </td>
                  <td className="mono">{t.lot_size}</td>
                  <td>
                    <span className="mono" style={{ color: 'var(--accent)', fontWeight: 600 }}>
                      {t.score}
                    </span>
                  </td>
                  <td>
                    <span className={`badge badge-${
                      t.stage === 'TRAILING' ? 'green' :
                      t.stage === 'TP1_HIT' || t.stage === 'BREAKEVEN' ? 'blue' : 'yellow'
                    }`}>
                      {t.stage}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {(!data?.trades || data.trades.length === 0) && (
            <div style={{ padding: '40px', textAlign: 'center', color: 'var(--text-muted)' }}>
              No active trades — the sniper is watching
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
