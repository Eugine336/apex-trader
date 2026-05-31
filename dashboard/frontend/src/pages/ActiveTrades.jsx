import React, { useState, useEffect } from 'react';
import { useOutletContext } from 'react-router-dom';
import { useApi } from '../hooks/useApi';

function fmtDuration(startMs) {
  const diff = Math.floor((Date.now() - startMs) / 1000);
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function derivePlatform(trade) {
  const cat = (trade.category || '').toLowerCase();
  if (cat === 'synthetic' || cat === 'crypto') return 'Deriv';
  return 'MT5';
}

export default function ActiveTrades() {
  const { state } = useOutletContext();
  const { data: restData } = useApi('/api/trades', 5000);
  const { data: scanData } = useApi('/api/scanner', 10000);

  const wsTrades = state.open_trades?.trades || [];
  const trades = wsTrades.length > 0 ? wsTrades : (restData?.trades || []);
  const instruments = scanData?.instruments || state.scanner?.instruments || [];
  const allSymbols = React.useMemo(() => instruments.map((i) => i.symbol), [instruments]);

  const [tick, setTick] = useState(0);
  const [tradeTimers] = useState(() => new Map());

  useEffect(() => {
    const t = setInterval(() => setTick((p) => p + 1), 1000);
    return () => clearInterval(t);
  }, []);

  trades.forEach((t) => {
    if (!tradeTimers.has(t.id)) tradeTimers.set(t.id, Date.now());
  });

  const totalPnl = trades.reduce((s, t) => s + (t.pnl_dollars || 0), 0);
  const avgScore = trades.length ? (trades.reduce((s, t) => s + (t.score || 0), 0) / trades.length).toFixed(0) : 0;
  const totalLots = trades.reduce((s, t) => s + (t.lot_size || 0), 0);

  return (
    <div>
      <div className="page-header">
        <h2>Active Trades</h2>
        <p>{trades.length} open position{trades.length !== 1 ? 's' : ''}</p>
      </div>

      <div className="card mb-20">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Instrument</th>
                <th>Platform</th>
                <th>Direction</th>
                <th className="right">Entry</th>
                <th className="right">Current</th>
                <th className="right">SL</th>
                <th className="right">TP1</th>
                <th className="right">TP2</th>
                <th className="right">Pips</th>
                <th className="right">P&L ($)</th>
                <th className="right">Lots</th>
                <th className="right">Score</th>
                <th>Stage</th>
                <th className="right">Duration</th>
              </tr>
            </thead>
            <tbody>
              {trades.length === 0 && (
                <tr><td colSpan={15} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 40 }}>No active trades — the sniper is watching</td></tr>
              )}
              {trades.map((t, i) => {
                const pnl = t.pnl_dollars || 0;
                const dir = (t.direction || '').toUpperCase();
                return (
                  <tr key={t.id} className={pnl > 0 ? 'row-profit' : pnl < 0 ? 'row-loss' : ''}>
                    <td>{i + 1}</td>
                    <td className="text-col" style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{t.instrument}</td>
                    <td className="text-col">{derivePlatform(t)}</td>
                    <td className={dir === 'LONG' ? 'dir-long' : 'dir-short'}>{dir}</td>
                    <td className="right">{(t.entry_price || 0).toFixed(5)}</td>
                    <td className="right">{(t.current_price || 0).toFixed(5)}</td>
                    <td className="right">{(t.stop_loss || 0).toFixed(5)}</td>
                    <td className="right">{(t.tp1 || 0).toFixed(5)}</td>
                    <td className="right">{(t.tp2 || 0).toFixed(5)}</td>
                    <td className={`right ${(t.pnl_pips || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>{(t.pnl_pips || 0).toFixed(1)}</td>
                    <td className={`right ${pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>${Math.abs(pnl).toFixed(2)}</td>
                    <td className="right">{(t.lot_size || 0).toFixed(2)}</td>
                    <td className="right">{t.score || 0}</td>
                    <td><span className={`badge ${t.stage === 'TRAILING' ? 'badge-green' : t.stage === 'TP1_HIT' || t.stage === 'BREAKEVEN' ? 'badge-blue' : 'badge-yellow'}`}>{t.stage}</span></td>
                    <td className="right">{fmtDuration(tradeTimers.get(t.id) || Date.now())}</td>
                  </tr>
                );
              })}
            </tbody>
            {trades.length > 0 && (
              <tfoot>
                <tr className="table-footer">
                  <td colSpan={9} style={{ textAlign: 'right' }}>TOTALS</td>
                  <td className="right">—</td>
                  <td className={`right ${totalPnl >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>${Math.abs(totalPnl).toFixed(2)}</td>
                  <td className="right">{totalLots.toFixed(2)}</td>
                  <td className="right">{avgScore}</td>
                  <td colSpan={2} />
                </tr>
              </tfoot>
            )}
          </table>
        </div>
      </div>

      <div className="card">
        <div className="card-header"><span className="card-title">Position Heat Map</span></div>
        <div className="pos-heatmap">
          {(allSymbols.length > 0 ? allSymbols : Array.from({ length: 66 }, (_, i) => `INST${i + 1}`)).map((sym) => {
            const openTrade = trades.find((t) => t.instrument === sym);
            let cls = '';
            if (openTrade) {
              cls = (openTrade.pnl_dollars || 0) > 0 ? 'pos-profit' : (openTrade.pnl_dollars || 0) < 0 ? 'pos-loss' : 'pos-open';
            }
            return <div key={sym} className={`pos-cell ${cls}`}>{sym}</div>;
          })}
        </div>
      </div>
    </div>
  );
}
