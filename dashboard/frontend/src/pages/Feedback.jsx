import React from 'react';
import { useApi } from '../hooks/useApi';

function wrColor(wr) {
  if (wr >= 0.55) return 'var(--green-bright)';
  if (wr < 0.45) return 'var(--red-bright)';
  return 'var(--yellow-bright)';
}

function calColor(gap) {
  if (gap === null || gap === undefined) return 'var(--text-muted)';
  if (Math.abs(gap) <= 0.1) return 'var(--green-bright)';
  if (gap > 0) return 'var(--red-bright)';   // overconfident
  return 'var(--accent-bright)';             // underconfident
}

export default function Feedback() {
  const { data, loading } = useApi('/api/outcome-feedback', 8000);
  const modules = data?.modules || [];
  const horizons = data?.horizons || [];

  return (
    <div>
      <div className="page-header">
        <h2>Outcome Feedback</h2>
        <p>The loop that closes: each closed trade's realised R links back to the modules that drove it — who's earning their keep, and who's overconfident.</p>
      </div>

      <div className="stat-grid mb-20" style={{ gridTemplateColumns: 'repeat(3, 1fr)' }}>
        <div className="stat-card">
          <div className="stat-label">Trades Linked</div>
          <div className="stat-value">{data?.total_trades || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Overall Win Rate</div>
          <div className="stat-value" style={{ color: wrColor(data?.overall_win_rate || 0) }}>
            {Math.round((data?.overall_win_rate || 0) * 100)}%
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Overall Avg R</div>
          <div className="stat-value" style={{ color: (data?.overall_avg_r || 0) >= 0 ? 'var(--green-bright)' : 'var(--red-bright)' }}>
            {(data?.overall_avg_r || 0) >= 0 ? '+' : ''}{Number(data?.overall_avg_r || 0).toFixed(2)}R
          </div>
        </div>
      </div>

      <div className="card mb-20">
        <div className="card-header"><span className="card-title">Per-module accuracy &amp; calibration</span></div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Module</th>
                <th className="right">Trades</th>
                <th className="right">Win Rate</th>
                <th className="right">Avg R</th>
                <th className="right">Avg Conf</th>
                <th className="right">Calibration Gap</th>
              </tr>
            </thead>
            <tbody>
              {modules.length === 0 && (
                <tr><td colSpan={6} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 48 }}>
                  {loading ? 'Loading…' : 'No completed trades linked yet — accuracy appears as trades close.'}
                </td></tr>
              )}
              {modules.map((m) => (
                <tr key={m.module}>
                  <td style={{ fontWeight: 600 }}>{m.module}</td>
                  <td className="right">{m.trades}</td>
                  <td className="right" style={{ color: wrColor(m.win_rate) }}>{Math.round(m.win_rate * 100)}%</td>
                  <td className="right" style={{ color: m.avg_r >= 0 ? 'var(--green-bright)' : 'var(--red-bright)' }}>
                    {m.avg_r >= 0 ? '+' : ''}{Number(m.avg_r).toFixed(2)}
                  </td>
                  <td className="right">{m.avg_confidence === null ? '—' : Number(m.avg_confidence).toFixed(2)}</td>
                  <td className="right" style={{ color: calColor(m.calibration_gap), fontFamily: "'JetBrains Mono', monospace" }}>
                    {m.calibration_gap === null ? '—' : `${m.calibration_gap > 0 ? '+' : ''}${Number(m.calibration_gap).toFixed(2)}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div style={{ fontSize: 11, color: 'var(--text-muted)', padding: '6px 12px' }}>
          Calibration gap = predicted confidence − realised win rate. ~0 well-calibrated · &gt;0 overconfident · &lt;0 underconfident.
        </div>
      </div>

      <div className="card">
        <div className="card-header"><span className="card-title">Per-horizon outcomes</span></div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Horizon</th>
                <th className="right">Trades</th>
                <th className="right">Win Rate</th>
                <th className="right">Avg R</th>
              </tr>
            </thead>
            <tbody>
              {horizons.length === 0 && (
                <tr><td colSpan={4} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>No data yet.</td></tr>
              )}
              {horizons.map((h) => (
                <tr key={h.horizon}>
                  <td style={{ fontWeight: 600 }}>{h.horizon}</td>
                  <td className="right">{h.trades}</td>
                  <td className="right" style={{ color: wrColor(h.win_rate) }}>{Math.round(h.win_rate * 100)}%</td>
                  <td className="right" style={{ color: h.avg_r >= 0 ? 'var(--green-bright)' : 'var(--red-bright)' }}>
                    {h.avg_r >= 0 ? '+' : ''}{Number(h.avg_r).toFixed(2)}
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
