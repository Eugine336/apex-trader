import React from 'react';
import { useApi } from '../hooks/useApi';

function fmtTime(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts);
    return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) + ' ' +
           d.toLocaleTimeString('en-GB', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch {
    return ts;
  }
}

export default function Reconciliation() {
  const { data, loading } = useApi('/api/reconciliation', 10000);

  const anomalies = data?.anomalies || [];

  return (
    <div>
      <div className="page-header">
        <h2>Exit Reconciliation</h2>
        <p>Broker-reported vs. derived exit reasons — discrepancies and anomalies</p>
      </div>

      <div className="stat-grid mb-20" style={{ gridTemplateColumns: 'repeat(2, 1fr)' }}>
        <div className="stat-card">
          <div className="stat-label">Discrepancies Found</div>
          <div className="stat-value" style={{ color: anomalies.length > 0 ? 'var(--yellow-bright)' : 'var(--green-bright)' }}>
            {anomalies.length}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Status</div>
          <div className="stat-value" style={{ fontSize: 16, color: anomalies.length === 0 ? 'var(--green-bright)' : 'var(--yellow-bright)' }}>
            {anomalies.length === 0 ? '✅ Clean' : '⚠ Review Needed'}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-header"><span className="card-title">Anomalous Trade Closes</span></div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Symbol</th>
                <th>Derived Reason</th>
                <th>Source</th>
                <th>Broker Reason</th>
                <th>Broker Comment</th>
                <th className="right">P&L ($)</th>
                <th style={{ width: 110 }}>Correlation</th>
              </tr>
            </thead>
            <tbody>
              {anomalies.length === 0 && (
                <tr><td colSpan={8} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 48 }}>
                  {loading ? 'Loading…' : 'No discrepancies — all exit reasons match.'}
                </td></tr>
              )}
              {anomalies.map((a, i) => {
                const mismatch = a.raw_broker_reason && a.exit_reason && a.exit_reason !== a.raw_broker_reason;
                return (
                  <tr key={a.event_id || i}>
                    <td style={{ fontSize: 12, color: 'var(--text-muted)' }}>{fmtTime(a.timestamp)}</td>
                    <td style={{ fontWeight: 600, fontFamily: 'var(--font-mono)' }}>{a.symbol || '—'}</td>
                    <td style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: mismatch ? 'var(--yellow-bright)' : 'var(--text-secondary)' }}>
                      {a.exit_reason || '(missing)'}
                    </td>
                    <td style={{ fontSize: 12, color: 'var(--text-muted)' }}>{a.exit_reason_source || '—'}</td>
                    <td style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: mismatch ? 'var(--text-primary)' : 'var(--text-muted)' }}>
                      {a.raw_broker_reason || '(none)'}
                    </td>
                    <td style={{ fontSize: 12, color: 'var(--text-muted)' }}>{a.raw_broker_comment || '—'}</td>
                    <td className={`right ${(a.pnl_dollars || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>
                      {a.pnl_dollars != null ? `$${Math.abs(a.pnl_dollars).toFixed(2)}` : '—'}
                    </td>
                    <td style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-muted)' }}>
                      {a.correlation_id ? a.correlation_id.slice(0, 16) : '—'}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
