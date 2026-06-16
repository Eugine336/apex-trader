import React from 'react';
import { useOutletContext } from 'react-router-dom';
import { useApi } from '../hooks/useApi';
import { EquityChart, PnLBarChart, WinRateDonut, SessionBarChart, HorizontalBarChart } from '../components/Charts';

export default function Performance() {
  const { state } = useOutletContext();
  const { data: perfData } = useApi('/api/performance', 0);
  const { data: mlData } = useApi('/api/ml', 0);
  const { data: sysData } = useApi('/api/system-performance', 0);

  const p = perfData || state.performance || {};
  const ml = mlData || {};
  const sys = sysData || {};
  const cache = sys.candle_cache || {};
  const cyc = sys.cycle || {};
  const par = sys.parallel_scan || {};

  const sessionData = React.useMemo(() => {
    if (!ml.session_stats) return [];
    return Object.entries(ml.session_stats).map(([name, s]) => ({ name, win_rate: s.win_rate || 0, trades: s.trades || 0 }));
  }, [ml.session_stats]);

  const pairData = React.useMemo(() => {
    if (!ml.pair_stats) return [];
    return Object.entries(ml.pair_stats)
      .map(([name, s]) => ({ name, trades: s.trades || 0, win_rate: s.win_rate || 0 }))
      .sort((a, b) => b.trades - a.trades)
      .slice(0, 10);
  }, [ml.pair_stats]);

  return (
    <div>
      <div className="page-header">
        <h2>Performance</h2>
        <p>Analytics and statistics</p>
      </div>

      <div className="stats-grid" style={{ gridTemplateColumns: 'repeat(8, 1fr)' }}>
        <div className="stat-card"><div className="stat-label">Win Rate</div><div className="stat-value positive">{(p.win_rate || 0).toFixed(1)}%</div></div>
        <div className="stat-card"><div className="stat-label">Total Trades</div><div className="stat-value neutral">{p.total_trades || 0}</div></div>
        <div className="stat-card"><div className="stat-label">Profit Factor</div><div className="stat-value neutral">{(p.profit_factor || 0).toFixed(2)}</div></div>
        <div className="stat-card"><div className="stat-label">Daily P&L</div><div className={`stat-value ${(p.daily_pnl || 0) >= 0 ? 'positive' : 'negative'}`}>${(p.daily_pnl || 0).toFixed(2)}</div></div>
        <div className="stat-card"><div className="stat-label">Weekly P&L</div><div className={`stat-value ${(p.weekly_pnl || 0) >= 0 ? 'positive' : 'negative'}`}>${(p.weekly_pnl || 0).toFixed(2)}</div></div>
        <div className="stat-card"><div className="stat-label">Monthly P&L</div><div className={`stat-value ${(p.monthly_pnl || 0) >= 0 ? 'positive' : 'negative'}`}>${(p.monthly_pnl || 0).toFixed(2)}</div></div>
        <div className="stat-card"><div className="stat-label">Avg Win</div><div className="stat-value positive">{(p.avg_win_pips || 0).toFixed(1)} pips</div></div>
        <div className="stat-card"><div className="stat-label">Avg Loss</div><div className="stat-value negative">{(p.avg_loss_pips || 0).toFixed(1)} pips</div></div>
      </div>

      <div className="grid-3 mb-20">
        <div className="card">
          <div className="card-header"><span className="card-title">Equity Curve</span></div>
          <EquityChart data={p.equity_curve} />
        </div>
        <div className="card">
          <div className="card-header"><span className="card-title">Daily P&L</span></div>
          <PnLBarChart data={p.pnl_history} />
        </div>
        <div className="card">
          <div className="card-header"><span className="card-title">Win / Loss</span></div>
          <WinRateDonut wins={p.win_count || p.wins || 0} losses={p.loss_count || p.losses || 0} />
        </div>
      </div>

      <div className="grid-2 mb-20">
        <div className="card">
          <div className="card-header"><span className="card-title">Win Rate by Session</span></div>
          <SessionBarChart data={sessionData} />
        </div>
        <div className="card">
          <div className="card-header"><span className="card-title">Trade Count by Instrument</span></div>
          <HorizontalBarChart data={pairData} />
        </div>
      </div>

      <div className="stats-grid mb-20" style={{ gridTemplateColumns: 'repeat(6, 1fr)' }}>
        <div className="stat-card"><div className="stat-label">Cache Hit Rate</div><div className="stat-value positive">{((cache.hit_rate || 0) * 100).toFixed(1)}%</div></div>
        <div className="stat-card"><div className="stat-label">Cache Hits / Miss</div><div className="stat-value neutral">{cache.hits || 0} / {cache.misses || 0}</div></div>
        <div className="stat-card"><div className="stat-label">Cached Frames</div><div className="stat-value neutral">{cache.entries || 0}</div></div>
        <div className="stat-card"><div className="stat-label">Cycle Avg</div><div className="stat-value neutral">{(cyc.avg_ms || 0).toFixed(0)} ms</div></div>
        <div className="stat-card"><div className="stat-label">Cycle p95</div><div className="stat-value neutral">{(cyc.p95_ms || 0).toFixed(0)} ms</div></div>
        <div className="stat-card"><div className="stat-label">Scan Workers</div><div className="stat-value neutral">{par.enabled ? (par.max_workers || 0) : 'off'}</div></div>
      </div>

      <div className="card mb-20">
        <div className="card-header"><span className="card-title">System Performance — Cache &amp; Latency</span></div>
        <div className="table-wrap">
          <table>
            <tbody>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Candle cache hit rate</td><td className="right">{((cache.hit_rate || 0) * 100).toFixed(1)}%</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Hits / Misses / Expired</td><td className="right">{cache.hits || 0} / {cache.misses || 0} / {cache.expired || 0}</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Cached frames in memory</td><td className="right">{cache.entries || 0}</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Scan cycle — last</td><td className="right">{(cyc.last_ms || 0).toFixed(1)} ms</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Scan cycle — avg / p50 / p95</td><td className="right">{(cyc.avg_ms || 0).toFixed(0)} / {(cyc.p50_ms || 0).toFixed(0)} / {(cyc.p95_ms || 0).toFixed(0)} ms</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Scan cycle — max ({cyc.samples || 0} samples)</td><td className="right">{(cyc.max_ms || 0).toFixed(0)} ms</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Parallel scan</td><td className="right">{par.enabled ? `on (${par.max_workers} workers)` : 'off'}</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Account-info cache</td><td className="right">{sys.account_info_cache_enabled ? 'on' : 'off'}</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <div className="card-header"><span className="card-title">Detailed Statistics</span></div>
        <div className="table-wrap">
          <table>
            <tbody>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Total Trades</td><td className="right">{p.total_trades || 0}</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Wins / Losses</td><td className="right">{p.win_count || p.wins || 0} / {p.loss_count || p.losses || 0}</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Win Rate</td><td className="right">{(p.win_rate || 0).toFixed(1)}%</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Profit Factor</td><td className="right">{(p.profit_factor || 0).toFixed(2)}</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Best Trade</td><td className="right pnl-positive">{(p.best_trade_pips || 0).toFixed(1)} pips</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Worst Trade</td><td className="right pnl-negative">{(p.worst_trade_pips || 0).toFixed(1)} pips</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Avg Win</td><td className="right pnl-positive">{(p.avg_win_pips || 0).toFixed(1)} pips</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Avg Loss</td><td className="right pnl-negative">{(p.avg_loss_pips || 0).toFixed(1)} pips</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Daily P&L</td><td className="right">${(p.daily_pnl || 0).toFixed(2)}</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Weekly P&L</td><td className="right">${(p.weekly_pnl || 0).toFixed(2)}</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Monthly P&L</td><td className="right">${(p.monthly_pnl || 0).toFixed(2)}</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Total P&L</td><td className="right">${(p.total_pnl || 0).toFixed(2)}</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Daily Trades</td><td className="right">{p.daily_trades || 0}</td></tr>
              <tr><td className="text-col" style={{ color: 'var(--text-muted)' }}>Avg R:R</td><td className="right">{p.avg_win_pips && p.avg_loss_pips ? (p.avg_win_pips / Math.abs(p.avg_loss_pips)).toFixed(2) : 'N/A'}</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
