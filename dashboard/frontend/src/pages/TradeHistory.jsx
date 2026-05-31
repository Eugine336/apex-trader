import React, { useState, useMemo } from 'react';
import { useApi } from '../hooks/useApi';
import { EquityChart, WinRateDonut } from '../components/Charts';

export default function TradeHistory() {
  const { data: histData } = useApi('/api/history', 0);
  const { data: perfData } = useApi('/api/performance', 0);
  const [filter, setFilter] = useState('ALL');
  const [search, setSearch] = useState('');
  const [sortCol, setSortCol] = useState(null);
  const [sortDir, setSortDir] = useState('desc');
  const [page, setPage] = useState(0);
  const perPage = 50;

  const trades = histData?.trades || [];
  const p = perfData || {};

  const filtered = useMemo(() => {
    let list = trades;
    if (filter === 'WIN') list = list.filter((t) => t.outcome === 'WIN');
    if (filter === 'LOSS') list = list.filter((t) => t.outcome === 'LOSS');
    if (search) list = list.filter((t) => (t.instrument || '').toLowerCase().includes(search.toLowerCase()));
    if (sortCol) {
      list = [...list].sort((a, b) => {
        const av = a[sortCol], bv = b[sortCol];
        if (typeof av === 'number') return sortDir === 'asc' ? av - bv : bv - av;
        return sortDir === 'asc' ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
      });
    }
    return list;
  }, [trades, filter, search, sortCol, sortDir]);

  const pageCount = Math.ceil(filtered.length / perPage);
  const paged = filtered.slice(page * perPage, (page + 1) * perPage);

  const handleSort = (col) => {
    if (sortCol === col) { setSortDir(sortDir === 'asc' ? 'desc' : 'asc'); }
    else { setSortCol(col); setSortDir('desc'); }
  };

  const fmtDate = (d) => {
    if (!d) return '—';
    const dt = new Date(d);
    return dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ' ' + dt.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
  };

  const fmtDur = (mins) => {
    if (!mins) return '—';
    if (mins < 60) return `${mins.toFixed(0)}m`;
    return `${Math.floor(mins / 60)}h ${Math.floor(mins % 60)}m`;
  };

  return (
    <div>
      <div className="page-header">
        <h2>Trade History</h2>
        <p>Closed positions and performance breakdown</p>
      </div>

      <div className="stats-grid" style={{ gridTemplateColumns: 'repeat(7, 1fr)' }}>
        <div className="stat-card"><div className="stat-label">Total Trades</div><div className="stat-value neutral">{p.total_trades || trades.length}</div></div>
        <div className="stat-card"><div className="stat-label">Win Rate</div><div className="stat-value positive">{(p.win_rate || 0).toFixed(1)}%</div></div>
        <div className="stat-card"><div className="stat-label">Profit Factor</div><div className="stat-value neutral">{(p.profit_factor || 0).toFixed(2)}</div></div>
        <div className="stat-card"><div className="stat-label">Avg Win</div><div className="stat-value positive">{(p.avg_win_pips || 0).toFixed(1)} pips</div></div>
        <div className="stat-card"><div className="stat-label">Avg Loss</div><div className="stat-value negative">{(p.avg_loss_pips || 0).toFixed(1)} pips</div></div>
        <div className="stat-card"><div className="stat-label">Best Trade</div><div className="stat-value positive">{(p.best_trade_pips || 0).toFixed(1)} pips</div></div>
        <div className="stat-card"><div className="stat-label">Worst Trade</div><div className="stat-value negative">{(p.worst_trade_pips || 0).toFixed(1)} pips</div></div>
      </div>

      <div className="filter-bar">
        {['ALL', 'WIN', 'LOSS'].map((f) => (
          <button key={f} className={`filter-btn ${filter === f ? 'active' : ''}`} onClick={() => { setFilter(f); setPage(0); }}>{f}</button>
        ))}
        <input className="filter-input" placeholder="Search instrument..." value={search} onChange={(e) => { setSearch(e.target.value); setPage(0); }} />
      </div>

      <div className="card mb-20">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="sortable" onClick={() => handleSort('opened_at')}>Date/Time</th>
                <th className="sortable" onClick={() => handleSort('instrument')}>Instrument</th>
                <th>Direction</th>
                <th className="right">Entry</th>
                <th className="right">Exit</th>
                <th className="right sortable" onClick={() => handleSort('pnl_pips')}>Pips</th>
                <th className="right sortable" onClick={() => handleSort('pnl_dollars')}>P&L ($)</th>
                <th className="right sortable" onClick={() => handleSort('duration_minutes')}>Duration</th>
                <th className="right sortable" onClick={() => handleSort('score')}>Score</th>
                <th>Outcome</th>
              </tr>
            </thead>
            <tbody>
              {paged.length === 0 && (
                <tr><td colSpan={10} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 40 }}>No trades match filter</td></tr>
              )}
              {paged.map((t) => {
                const pnl = t.pnl_dollars || 0;
                const dir = (t.direction || '').toUpperCase();
                return (
                  <tr key={t.id}>
                    <td className="text-col">{fmtDate(t.opened_at)}</td>
                    <td className="text-col" style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{t.instrument}</td>
                    <td className={dir === 'LONG' ? 'dir-long' : 'dir-short'}>{dir}</td>
                    <td className="right">{(t.entry_price || 0).toFixed(5)}</td>
                    <td className="right">{(t.exit_price || 0).toFixed(5)}</td>
                    <td className={`right ${(t.pnl_pips || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>{(t.pnl_pips || 0).toFixed(1)}</td>
                    <td className={`right ${pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>${Math.abs(pnl).toFixed(2)}</td>
                    <td className="right">{fmtDur(t.duration_minutes)}</td>
                    <td className="right">{t.score || 0}</td>
                    <td><span className={`badge ${t.outcome === 'WIN' ? 'badge-green' : 'badge-red'}`}>{t.outcome}</span></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {pageCount > 1 && (
          <div className="pagination">
            <button className="btn" disabled={page === 0} onClick={() => setPage(page - 1)}>← Prev</button>
            <span>Page {page + 1} of {pageCount}</span>
            <button className="btn" disabled={page >= pageCount - 1} onClick={() => setPage(page + 1)}>Next →</button>
          </div>
        )}
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-header"><span className="card-title">Cumulative P&L</span></div>
          <EquityChart data={p.equity_curve} />
        </div>
        <div className="card">
          <div className="card-header"><span className="card-title">Win / Loss Distribution</span></div>
          <WinRateDonut wins={p.win_count || p.wins || 0} losses={p.loss_count || p.losses || 0} />
        </div>
      </div>
    </div>
  );
}
