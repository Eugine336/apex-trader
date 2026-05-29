import React from 'react';
import { useApi } from '../hooks/useApi';
import TradeCard from '../components/TradeCard';
import { EquityChart, MiniLine } from '../components/Charts';
import ScoreBar from '../components/ScoreBar';

export default function Overview() {
  const { data: status } = useApi('/api/status', 3000);
  const { data: trades } = useApi('/api/trades', 3000);
  const { data: perf } = useApi('/api/performance', 5000);
  const { data: scanner } = useApi('/api/scanner', 5000);

  if (!status) return <div style={{ color: 'var(--text-muted)' }}>Loading...</div>;

  return (
    <div>
      <div className="page-header">
        <h2>Overview</h2>
        <p>Real-time system snapshot</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-label">Win Rate</div>
          <div className={`stat-value ${status.win_rate >= 70 ? 'positive' : 'negative'}`}>
            {status.win_rate}%
          </div>
          <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '4px' }}>
            {status.win_count}W / {status.loss_count}L ({status.total_trades} total)
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Today's P&L</div>
          <div className={`stat-value ${status.daily_pnl >= 0 ? 'positive' : 'negative'}`}>
            ${status.daily_pnl?.toFixed(2)}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Account Balance</div>
          <div className="stat-value neutral">
            ${status.account_balance?.toLocaleString()}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Total P&L</div>
          <div className={`stat-value ${status.total_pnl >= 0 ? 'positive' : 'negative'}`}>
            ${status.total_pnl?.toFixed(2)}
          </div>
        </div>
      </div>

      <div className="grid-2 mb-24">
        <div className="card">
          <div className="card-header">
            <span className="card-title">Equity Curve</span>
          </div>
          {perf?.equity_curve && <EquityChart data={perf.equity_curve} />}
        </div>

        <div className="card">
          <div className="card-header">
            <span className="card-title">Active Trades ({trades?.count || 0})</span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            {trades?.trades?.map((t) => (
              <TradeCard key={t.id} trade={t} />
            ))}
            {(!trades?.trades || trades.trades.length === 0) && (
              <div style={{ color: 'var(--text-muted)', fontSize: '13px', padding: '20px', textAlign: 'center' }}>
                No active trades
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <span className="card-title">Top Scanner Results</span>
          <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
            {scanner?.ready_count || 0} ready · {scanner?.watchlist_count || 0} watchlist
          </span>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
          {scanner?.instruments?.slice(0, 10).map((inst) => (
            <div key={inst.symbol} style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
              <span className="mono" style={{ minWidth: '70px', fontSize: '12px', fontWeight: 600 }}>
                {inst.symbol}
              </span>
              <span className={inst.direction === 'LONG' ? 'dir-long' :
                               inst.direction === 'SHORT' ? 'dir-short' : 'dir-neutral'}
                    style={{ fontSize: '11px', minWidth: '50px' }}>
                {inst.direction}
              </span>
              <div style={{ flex: 1 }}>
                <ScoreBar score={inst.score} />
              </div>
              <span className={`badge badge-${
                inst.status === 'READY' ? 'green' :
                inst.status === 'WATCHLIST' ? 'yellow' : 'red'
              }`} style={{ minWidth: '70px', textAlign: 'center' }}>
                {inst.status}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
