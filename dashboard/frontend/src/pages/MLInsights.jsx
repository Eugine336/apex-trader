import React from 'react';
import { useApi } from '../hooks/useApi';
import { WeightAdjustmentChart } from '../components/Charts';

function rating(wr) {
  if (wr >= 70) return { text: 'STRONG', cls: 'badge-green' };
  if (wr >= 55) return { text: 'MODERATE', cls: 'badge-yellow' };
  return { text: 'WEAK', cls: 'badge-red' };
}

export default function MLInsights() {
  const { data } = useApi('/api/ml', 0);

  if (!data) {
    return (
      <div>
        <div className="page-header"><h2>ML Insights</h2><p>Loading adaptive learner data...</p></div>
        <div className="skeleton skeleton-block mb-20" />
        <div className="grid-2 mb-20"><div className="skeleton skeleton-block" /><div className="skeleton skeleton-block" /></div>
      </div>
    );
  }

  const regimes = data.regime_stats ? Object.entries(data.regime_stats) : [];
  const sessions = data.session_stats ? Object.entries(data.session_stats) : [];
  const pairs = data.pair_stats
    ? Object.entries(data.pair_stats).sort((a, b) => (b[1].win_rate || 0) - (a[1].win_rate || 0))
    : [];

  return (
    <div>
      <div className="page-header">
        <h2>ML Insights</h2>
        <p>Adaptive learner adjustments and analytics</p>
      </div>

      <div className="card mb-20">
        <div className="card-header">
          <span className="card-title">Score Weight Adjustments</span>
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>Positive = ML increased factor importance</span>
        </div>
        <WeightAdjustmentChart data={data.score_adjustments} />
      </div>

      <div className="grid-2 mb-20">
        <div className="card">
          <div className="card-header"><span className="card-title">Regime Performance</span></div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Regime</th>
                  <th className="right">Trades</th>
                  <th className="right">Win Rate</th>
                  <th>Recommendation</th>
                </tr>
              </thead>
              <tbody>
                {regimes.length === 0 && <tr><td colSpan={4} style={{ textAlign: 'center', color: 'var(--text-muted)' }}>No regime data</td></tr>}
                {regimes.map(([name, s]) => (
                  <tr key={name}>
                    <td className="text-col" style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{name}</td>
                    <td className="right">{s.trades || 0}</td>
                    <td className="right" style={{ color: (s.win_rate || 0) >= 60 ? 'var(--green-bright)' : (s.win_rate || 0) >= 50 ? 'var(--yellow-bright)' : 'var(--red-bright)' }}>
                      {(s.win_rate || 0).toFixed(1)}%
                    </td>
                    <td>
                      <span className={`badge ${(s.recommendation || '').toLowerCase() === 'trade' ? 'badge-green' : 'badge-red'}`}>
                        {(s.recommendation || 'N/A').toUpperCase()}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card">
          <div className="card-header"><span className="card-title">Session Performance</span></div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Session</th>
                  <th className="right">Trades</th>
                  <th className="right">Win Rate</th>
                  <th>Aggression</th>
                </tr>
              </thead>
              <tbody>
                {sessions.length === 0 && <tr><td colSpan={4} style={{ textAlign: 'center', color: 'var(--text-muted)' }}>No session data</td></tr>}
                {sessions.map(([name, s]) => (
                  <tr key={name}>
                    <td className="text-col" style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{name}</td>
                    <td className="right">{s.trades || 0}</td>
                    <td className="right" style={{ color: (s.win_rate || 0) >= 60 ? 'var(--green-bright)' : (s.win_rate || 0) >= 50 ? 'var(--yellow-bright)' : 'var(--red-bright)' }}>
                      {(s.win_rate || 0).toFixed(1)}%
                    </td>
                    <td>
                      <span className={`badge ${(s.aggression || '').toLowerCase() === 'high' ? 'badge-yellow' : (s.aggression || '').toLowerCase() === 'low' ? 'badge-muted' : 'badge-green'}`}>
                        {(s.aggression || 'N/A').toUpperCase()}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-header"><span className="card-title">Pair Performance</span></div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Pair</th>
                <th className="right">Trades</th>
                <th className="right">Win Rate</th>
                <th className="right">Size Multiplier</th>
                <th>Rating</th>
              </tr>
            </thead>
            <tbody>
              {pairs.length === 0 && <tr><td colSpan={5} style={{ textAlign: 'center', color: 'var(--text-muted)' }}>No pair data</td></tr>}
              {pairs.map(([name, s]) => {
                const r = rating(s.win_rate || 0);
                const mult = s.size_mult || 1;
                return (
                  <tr key={name}>
                    <td style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{name}</td>
                    <td className="right">{s.trades || 0}</td>
                    <td className="right" style={{ color: (s.win_rate || 0) >= 60 ? 'var(--green-bright)' : (s.win_rate || 0) >= 50 ? 'var(--yellow-bright)' : 'var(--red-bright)' }}>
                      {(s.win_rate || 0).toFixed(1)}%
                    </td>
                    <td className="right" style={{ color: mult >= 1 ? 'var(--green-bright)' : 'var(--red-bright)' }}>
                      {mult.toFixed(1)}x
                    </td>
                    <td><span className={`badge ${r.cls}`}>{r.text}</span></td>
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
