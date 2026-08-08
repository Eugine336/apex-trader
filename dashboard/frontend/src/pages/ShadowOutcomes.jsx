import React, { useState, useMemo } from 'react';
import { useApi } from '../hooks/useApi';

function fmtR(r) {
  if (r == null) return '—';
  const v = Number(r);
  return (v >= 0 ? '+' : '') + v.toFixed(2) + 'R';
}

function outcomeBadge(outcome) {
  switch ((outcome || '').toUpperCase()) {
    case 'WIN':     return 'badge-green';
    case 'LOSS':    return 'badge-red';
    case 'EXPIRED': return 'badge-muted';
    case 'BE':      return 'badge-yellow';
    case 'PARTIAL': return 'badge-blue';
    default:        return 'badge-muted';
  }
}

function fmtTs(ms) {
  if (!ms) return '—';
  try {
    const d = new Date(Number(ms));
    return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) + ' ' +
           d.toLocaleTimeString('en-GB', { hour12: false, hour: '2-digit', minute: '2-digit' });
  } catch {
    return '—';
  }
}

export default function ShadowOutcomes() {
  const { data, loading } = useApi('/api/shadow', 10000);
  const [gateFilter, setGateFilter] = useState('ALL');

  const gates = data?.gates || [];
  const contracts = data?.contracts || [];
  const summary = data?.summary || {};

  const filteredContracts = useMemo(() => {
    if (gateFilter === 'ALL') return contracts;
    return contracts.filter(c => c.rejecting_gate === gateFilter);
  }, [contracts, gateFilter]);

  const gateNames = ['ALL', ...gates.map(g => g.gate)];

  return (
    <div>
      <div className="page-header">
        <h2>Shadow Outcomes</h2>
        <p>Rejected/skipped setup counterfactual results — did they win or lose?</p>
      </div>

      <div className="stat-grid mb-20" style={{ gridTemplateColumns: 'repeat(4, 1fr)' }}>
        <div className="stat-card">
          <div className="stat-label">Pending</div>
          <div className="stat-value">{summary.PENDING || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Resolved</div>
          <div className="stat-value positive">{summary.RESOLVED || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Expired</div>
          <div className="stat-value" style={{ color: 'var(--text-muted)' }}>{summary.EXPIRED || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Total Contracts</div>
          <div className="stat-value">{(summary.PENDING || 0) + (summary.RESOLVED || 0) + (summary.EXPIRED || 0)}</div>
        </div>
      </div>

      {gates.length > 0 && (
        <div className="card mb-20">
          <div className="card-header"><span className="card-title">Outcomes by Rejecting Gate</span></div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Gate</th>
                  <th className="right">Total</th>
                  <th className="right">Wins</th>
                  <th className="right">Losses</th>
                  <th className="right">Expired</th>
                  <th className="right">Win Rate</th>
                  <th className="right">Avg R</th>
                </tr>
              </thead>
              <tbody>
                {gates.map(g => (
                  <tr key={g.gate} onClick={() => setGateFilter(g.gate)} style={{ cursor: 'pointer' }}>
                    <td style={{ fontWeight: 600, color: 'var(--text-primary)', fontFamily: 'var(--font-mono)' }}>{g.gate}</td>
                    <td className="right">{g.total}</td>
                    <td className="right pnl-positive">{g.WIN || 0}</td>
                    <td className="right pnl-negative">{g.LOSS || 0}</td>
                    <td className="right" style={{ color: 'var(--text-muted)' }}>{g.EXPIRED || 0}</td>
                    <td className="right" style={{ fontWeight: 600 }}>{g.win_rate}%</td>
                    <td className="right" style={{ fontFamily: 'var(--font-mono)' }}>{fmtR(g.avg_r)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="filter-bar" style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 16 }}>
        {gateNames.map(g => (
          <button key={g} className={`filter-btn ${gateFilter === g ? 'active' : ''}`} onClick={() => setGateFilter(g)}>{g}</button>
        ))}
      </div>

      <div className="card">
        <div className="card-header"><span className="card-title">Recent Shadow Contracts</span></div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Symbol</th>
                <th>Dir</th>
                <th className="right">Entry</th>
                <th className="right">SL</th>
                <th className="right">TP1</th>
                <th>Gate</th>
                <th>Status</th>
                <th>Outcome</th>
                <th className="right">R</th>
                <th>Granularity</th>
                <th style={{ width: 110 }}>Correlation</th>
              </tr>
            </thead>
            <tbody>
              {filteredContracts.length === 0 && (
                <tr><td colSpan={12} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 40 }}>
                  {loading ? 'Loading…' : 'No shadow contracts yet.'}
                </td></tr>
              )}
              {filteredContracts.map(c => (
                <tr key={c.contract_id}>
                  <td style={{ fontSize: 12, color: 'var(--text-muted)' }}>{fmtTs(c.timestamp)}</td>
                  <td style={{ fontWeight: 600, fontFamily: 'var(--font-mono)' }}>{c.symbol}</td>
                  <td className={c.direction === 'BUY' ? 'dir-long' : 'dir-short'}>{c.direction}</td>
                  <td className="right">{(c.entry_price || 0).toFixed(5)}</td>
                  <td className="right">{(c.stop_loss || 0).toFixed(5)}</td>
                  <td className="right">{(c.tp1 || 0).toFixed(5)}</td>
                  <td style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }}>{c.rejecting_gate}</td>
                  <td><span className={`badge ${c.status === 'RESOLVED' ? 'badge-green' : c.status === 'PENDING' ? 'badge-blue' : 'badge-muted'}`}>{c.status}</span></td>
                  <td>{c.outcome ? <span className={`badge ${outcomeBadge(c.outcome)}`}>{c.outcome}</span> : '—'}</td>
                  <td className="right" style={{ fontFamily: 'var(--font-mono)' }}>{fmtR(c.r_multiple)}</td>
                  <td style={{ fontSize: 11, color: 'var(--text-muted)' }}>{c.resolution_granularity || '—'}</td>
                  <td style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-muted)' }}>
                    {c.correlation_id ? c.correlation_id.slice(0, 16) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
