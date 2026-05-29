import React from 'react';

export default function TradeCard({ trade }) {
  const isProfit = trade.pnl_dollars >= 0;

  return (
    <div className="card" style={{ padding: '14px 16px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span style={{ fontWeight: 700, fontSize: '14px' }}>{trade.instrument}</span>
          <span className={trade.direction === 'LONG' ? 'dir-long' : 'dir-short'}
                style={{ fontSize: '12px', fontWeight: 600 }}>
            {trade.direction}
          </span>
        </div>
        <span className={`badge badge-${
          trade.stage === 'TRAILING' ? 'green' :
          trade.stage === 'TP1_HIT' || trade.stage === 'BREAKEVEN' ? 'blue' : 'yellow'
        }`}>
          {trade.stage}
        </span>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: '8px', fontSize: '12px' }}>
        <div>
          <div style={{ color: 'var(--text-muted)' }}>Entry</div>
          <div className="mono">{trade.entry_price}</div>
        </div>
        <div>
          <div style={{ color: 'var(--text-muted)' }}>Current</div>
          <div className="mono">{trade.current_price}</div>
        </div>
        <div>
          <div style={{ color: 'var(--text-muted)' }}>P&L</div>
          <div className={`mono ${isProfit ? 'pnl-positive' : 'pnl-negative'}`}>
            ${trade.pnl_dollars?.toFixed(2)}
          </div>
        </div>
        <div>
          <div style={{ color: 'var(--text-muted)' }}>Score</div>
          <div className="mono" style={{ color: 'var(--accent)' }}>{trade.score}</div>
        </div>
      </div>
    </div>
  );
}
