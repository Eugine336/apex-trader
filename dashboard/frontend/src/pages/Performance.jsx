import React from 'react';
import { useApi } from '../hooks/useApi';
import { EquityChart, PnLBarChart, WinRateDonut } from '../components/Charts';

export default function Performance() {
  const { data } = useApi('/api/performance', 5000);

  if (!data) return <div style={{ color: 'var(--text-muted)' }}>Loading...</div>;

  return (
    <div>
      <div className="page-header">
        <h2>Performance</h2>
        <p>Win rate, P&L, equity curve, and drawdown analysis</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-label">Win Rate</div>
          <div className={`stat-value ${data.win_rate >= 70 ? 'positive' : 'negative'}`}>
            {data.win_rate}%
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Total Trades</div>
          <div className="stat-value neutral">{data.total_trades}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Daily P&L</div>
          <div className={`stat-value ${data.daily_pnl >= 0 ? 'positive' : 'negative'}`}>
            ${data.daily_pnl?.toFixed(2)}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Weekly P&L</div>
          <div className={`stat-value ${data.weekly_pnl >= 0 ? 'positive' : 'negative'}`}>
            ${data.weekly_pnl?.toFixed(2)}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Monthly P&L</div>
          <div className={`stat-value ${data.monthly_pnl >= 0 ? 'positive' : 'negative'}`}>
            ${data.monthly_pnl?.toFixed(2)}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Total P&L</div>
          <div className={`stat-value ${data.total_pnl >= 0 ? 'positive' : 'negative'}`}>
            ${data.total_pnl?.toFixed(2)}
          </div>
        </div>
      </div>

      <div className="grid-2 mb-24">
        <div className="card">
          <div className="card-header">
            <span className="card-title">Equity Curve</span>
          </div>
          {data.equity_curve && <EquityChart data={data.equity_curve} />}
        </div>
        <div className="card">
          <div className="card-header">
            <span className="card-title">Win / Loss Distribution</span>
          </div>
          <WinRateDonut wins={data.win_count} losses={data.loss_count} />
          <div style={{ textAlign: 'center', marginTop: '12px', fontSize: '13px' }}>
            <span style={{ color: 'var(--green)' }}>{data.win_count} wins</span>
            {' · '}
            <span style={{ color: 'var(--red)' }}>{data.loss_count} losses</span>
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <span className="card-title">Daily P&L</span>
        </div>
        {data.pnl_history && <PnLBarChart data={data.pnl_history} />}
      </div>
    </div>
  );
}
