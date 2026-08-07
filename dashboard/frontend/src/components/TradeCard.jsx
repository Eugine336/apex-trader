import React from 'react';
import ScoreBar from './ScoreBar';

const STAGE_BADGE = {
  TRAILING: 'badge-green',
  TP1_HIT: 'badge-blue',
  BREAKEVEN: 'badge-blue',
  OPEN: 'badge-yellow',
};

export default function TradeCard({ trade }) {
  if (!trade) return null;
  const pnl = trade.pnl_dollars || 0;
  const pips = trade.pnl_pips || 0;
  const dir = (trade.direction || '').toUpperCase();

  return (
    <div className="trade-card">
      <div className="trade-card-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className="trade-card-pair">{trade.instrument}</span>
          <span className={dir === 'LONG' ? 'dir-long' : 'dir-short'} style={{ fontWeight: 700, fontSize: 12 }}>
            {dir}
          </span>
        </div>
        <span className={`badge ${STAGE_BADGE[trade.stage] || 'badge-yellow'}`}>{trade.stage}</span>
      </div>
      <div className="trade-card-grid">
        <div className="trade-card-field">
          <div className="tc-label">Entry</div>
          <div className="tc-val">{(trade.entry_price || 0).toFixed(5)}</div>
        </div>
        <div className="trade-card-field">
          <div className="tc-label">Current</div>
          <div className="tc-val">{(trade.current_price || 0).toFixed(5)}</div>
        </div>
        <div className="trade-card-field">
          <div className="tc-label">SL</div>
          <div className="tc-val">{(trade.stop_loss || 0).toFixed(5)}</div>
        </div>
        <div className="trade-card-field">
          <div className="tc-label">TP1</div>
          <div className="tc-val">{(trade.tp1 || 0).toFixed(5)}</div>
        </div>
        <div className="trade-card-field">
          <div className="tc-label">TP2</div>
          <div className="tc-val">{(trade.tp2 || 0).toFixed(5)}</div>
        </div>
        <div className="trade-card-field">
          <div className="tc-label">Lots</div>
          <div className="tc-val">{(trade.lot_size || 0).toFixed(2)}</div>
        </div>
      </div>
      <div className="trade-card-footer">
        <span style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 600, fontSize: 13, color: pnl >= 0 ? 'var(--green-bright)' : 'var(--red-bright)' }}>
          {pips >= 0 ? '+' : ''}{pips.toFixed(1)} pips / {pnl >= 0 ? '+' : ''}${Math.abs(pnl).toFixed(2)}
        </span>
        <div style={{ width: 100 }}>
          <ScoreBar score={trade.score || 0} />
        </div>
      </div>
    </div>
  );
}
