import React from 'react';
import { useOutletContext } from 'react-router-dom';
import { useApi } from '../hooks/useApi';
import TradeCard from '../components/TradeCard';
import DepartmentGrid from '../components/DepartmentGrid';
import { EquityChart, PnLBarChart } from '../components/Charts';

function StatTile({ label, value, sub, cls }) {
  return (
    <div className="stat-card">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${cls || 'neutral'}`}>{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

export default function Overview() {
  const { state } = useOutletContext();
  const s = state.status || {};
  const trades = state.open_trades?.trades || [];
  const perf = state.performance || {};
  const scanner = state.scanner || {};

  const { data: perfData } = useApi('/api/performance', 0);
  const { data: scanData } = useApi('/api/scanner', 0);
  // Department health: live via WS slow channel, with a polled fallback so the
  // org board populates even before the first WS push.
  const { data: deptData } = useApi('/api/departments', 15000);
  const departments = state.departments || deptData;

  const p = perfData || perf;
  const sc = scanData || scanner;
  const instruments = sc.instruments || [];

  const dailyPnl = s.daily_pnl || p.daily_pnl || 0;
  const weeklyPnl = p.weekly_pnl || 0;
  const totalPnl = s.total_pnl || p.total_pnl || 0;
  const winRate = s.win_rate || p.win_rate || 0;

  const historyByHour = React.useMemo(() => {
    const hours = Array.from({ length: 24 }, (_, i) => ({ date: `${String(i).padStart(2, '0')}:00`, pnl: 0 }));
    return hours;
  }, []);

  return (
    <div>
      <div className="page-header">
        <h2>Overview</h2>
        <p>The 9-department trading organism — live health at a glance</p>
      </div>

      <DepartmentGrid data={departments} />

      <div className="stats-grid" style={{ gridTemplateColumns: 'repeat(6, 1fr)' }}>
        <StatTile
          label="Win Rate"
          value={`${winRate.toFixed(1)}%`}
          sub={`${s.win_count || p.win_count || p.wins || 0}W / ${s.loss_count || p.loss_count || p.losses || 0}L`}
          cls="positive"
        />
        <StatTile
          label="Daily P&L"
          value={`${dailyPnl >= 0 ? '+' : ''}$${Math.abs(dailyPnl).toFixed(2)}`}
          sub={`${p.daily_trades || 0} trades today`}
          cls={dailyPnl >= 0 ? 'positive' : 'negative'}
        />
        <StatTile
          label="Weekly P&L"
          value={`${weeklyPnl >= 0 ? '+' : ''}$${Math.abs(weeklyPnl).toFixed(2)}`}
          cls={weeklyPnl >= 0 ? 'positive' : 'negative'}
        />
        <StatTile
          label="Total P&L"
          value={`${totalPnl >= 0 ? '+' : ''}$${Math.abs(totalPnl).toFixed(2)}`}
          sub={`${s.total_trades || p.total_trades || 0} total trades`}
          cls={totalPnl >= 0 ? 'positive' : 'negative'}
        />
        <StatTile
          label="Open Trades"
          value={s.open_trade_count || trades.length || 0}
          sub={`${s.consecutive_wins || 0}W streak / ${s.consecutive_losses || 0}L streak`}
        />
        <StatTile
          label="Account Balance"
          value={`$${(s.account_balance || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}`}
          sub={`MT5 $${(s.mt5_balance || 0).toLocaleString()} · Deriv $${(s.deriv_balance || 0).toLocaleString()}`}
        />
      </div>

      <div className="grid-60-40 mb-20">
        <div className="card">
          <div className="card-header"><span className="card-title">Equity Curve</span></div>
          <EquityChart data={p.equity_curve} />
        </div>
        <div className="card">
          <div className="card-header"><span className="card-title">Active Trades</span></div>
          {trades.length === 0 ? (
            <div style={{ color: 'var(--text-muted)', fontSize: 12, padding: '20px 0', textAlign: 'center' }}>
              No active positions — scanning markets
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {trades.map((t) => <TradeCard key={t.id} trade={t} />)}
            </div>
          )}
        </div>
      </div>

      <div className="card mb-20">
        <div className="card-header">
          <span className="card-title">Scanner Heat Strip</span>
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            {sc.ready_count || 0} ready · {sc.watchlist_count || 0} watchlist · {sc.total_count || instruments.length} total
          </span>
        </div>
        <div className="heat-strip">
          {instruments.map((inst) => {
            const dir = (inst.direction || '').toUpperCase();
            const arrow = dir === 'LONG' ? '↑' : dir === 'SHORT' ? '↓' : '—';
            const status = (inst.status || '').toLowerCase();
            return (
              <div key={inst.symbol} className={`heat-cell ${status}`}>
                <div className="heat-cell-sym">{inst.symbol}</div>
                <div className={`heat-cell-dir ${dir === 'LONG' ? 'dir-long' : dir === 'SHORT' ? 'dir-short' : 'dir-neutral'}`}>{arrow}</div>
                <div className="heat-cell-score">{inst.score}</div>
              </div>
            );
          })}
          {instruments.length === 0 && <div style={{ color: 'var(--text-muted)', fontSize: 12, padding: 16 }}>Awaiting first scan cycle</div>}
        </div>
      </div>

      <div className="grid-2 mb-20">
        <div className="card">
          <div className="card-header"><span className="card-title">Today's P&L by Hour</span></div>
          <PnLBarChart data={historyByHour} />
        </div>
        <div className="card">
          <div className="card-header"><span className="card-title">Rejection Log</span></div>
          <div style={{ color: 'var(--text-muted)', fontSize: 12, padding: '20px 0', textAlign: 'center' }}>
            Awaiting first scan cycle
          </div>
        </div>
      </div>
    </div>
  );
}
