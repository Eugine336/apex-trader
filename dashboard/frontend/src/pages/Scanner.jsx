import React, { useState, useMemo } from 'react';
import { useOutletContext } from 'react-router-dom';
import { useApi } from '../hooks/useApi';
import ScoreBar from '../components/ScoreBar';
import { FactorHeatmap } from '../components/Charts';

const CATS = ['ALL', 'FOREX', 'COMMODITY', 'INDEX', 'SYNTHETIC'];
const FACTORS = ['structure', 'fvg', 'ob', 'liquidity', 'sweep', 'session', 'strength'];

function factorColor(v) {
  if (v > 8) return 'var(--green-bright)';
  if (v > 4) return 'var(--yellow-bright)';
  return 'var(--text-muted)';
}

export default function Scanner() {
  const { state } = useOutletContext();
  const { data: restData } = useApi('/api/scanner', 0);
  const [catFilter, setCatFilter] = useState('ALL');

  const sc = state.scanner || restData || {};
  const instruments = sc.instruments || [];

  const filtered = useMemo(() => {
    let list = instruments;
    if (catFilter !== 'ALL') list = list.filter((i) => (i.category || '').toUpperCase() === catFilter);
    return [...list].sort((a, b) => (b.score || 0) - (a.score || 0));
  }, [instruments, catFilter]);

  return (
    <div>
      <div className="page-header">
        <h2>Scanner</h2>
        <p>{sc.ready_count || 0} ready · {sc.watchlist_count || 0} watchlist · {sc.total_count || instruments.length} total</p>
      </div>

      <div className="filter-bar">
        {CATS.map((c) => (
          <button key={c} className={`filter-btn ${catFilter === c ? 'active' : ''}`} onClick={() => setCatFilter(c)}>{c}</button>
        ))}
      </div>

      <div className="card mb-20">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Name</th>
                <th>Cat</th>
                <th>Dir</th>
                <th className="right" style={{ minWidth: 120 }}>Score</th>
                <th>Status</th>
                {FACTORS.map((f) => <th key={f} className="right">{f.toUpperCase()}</th>)}
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 && (
                <tr><td colSpan={13} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 40 }}>No instruments match filter</td></tr>
              )}
              {filtered.map((inst) => {
                const dir = (inst.direction || '').toUpperCase();
                const status = (inst.status || '').toUpperCase();
                const isReady = status === 'READY';
                return (
                  <tr key={inst.symbol} style={isReady ? { borderLeft: '3px solid var(--green-bright)' } : undefined}>
                    <td style={{ fontWeight: 700, color: 'var(--text-primary)' }}>{inst.symbol}</td>
                    <td className="text-col">{inst.name}</td>
                    <td><span className="badge badge-muted">{(inst.category || '').toUpperCase()}</span></td>
                    <td className={dir === 'LONG' ? 'dir-long' : dir === 'SHORT' ? 'dir-short' : 'dir-neutral'}>{dir || '—'}</td>
                    <td className="right" style={{ minWidth: 120 }}><ScoreBar score={inst.score || 0} /></td>
                    <td>
                      <span className={`badge ${status === 'READY' ? 'badge-green' : status === 'WATCHLIST' ? 'badge-yellow' : 'badge-muted'}`}>{status}</span>
                    </td>
                    {FACTORS.map((f) => {
                      const v = inst.factors?.[f] ?? 0;
                      return <td key={f} className="right" style={{ color: factorColor(v) }}>{v}</td>;
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <div className="card-header"><span className="card-title">Factor Heatmap — Top 20</span></div>
        <FactorHeatmap instruments={instruments} />
      </div>
    </div>
  );
}
