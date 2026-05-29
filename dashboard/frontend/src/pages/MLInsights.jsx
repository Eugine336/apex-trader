import React from 'react';
import { useApi } from '../hooks/useApi';

function AdjustmentRow({ factor, adjustment }) {
  const color = adjustment > 0 ? 'var(--green)' : adjustment < 0 ? 'var(--red)' : 'var(--text-muted)';
  const prefix = adjustment > 0 ? '+' : '';
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 12px',
                   background: 'var(--bg-hover)', borderRadius: '6px', fontSize: '13px' }}>
      <span style={{ textTransform: 'capitalize' }}>{factor.replace('_', ' ')}</span>
      <span className="mono" style={{ fontWeight: 600, color }}>{prefix}{adjustment}</span>
    </div>
  );
}

function StatTable({ title, stats, valueKey, labelKey, extraKey }) {
  return (
    <div className="card">
      <div className="card-header"><span className="card-title">{title}</span></div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
        {Object.entries(stats).map(([key, val]) => {
          const winRate = val.win_rate || val[valueKey] || 0;
          const color = winRate >= 80 ? 'var(--green)' : winRate >= 60 ? 'var(--yellow)' : 'var(--red)';
          return (
            <div key={key} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                                     padding: '8px 12px', background: 'var(--bg-hover)', borderRadius: '6px' }}>
              <div>
                <div style={{ fontSize: '13px', fontWeight: 600 }}>{key.replace(/_/g, ' ')}</div>
                <div style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                  {val.trades} trades
                  {val[extraKey] && ` · ${val[extraKey]}`}
                  {val.aggression && ` · ${val.aggression}`}
                  {val.recommendation && ` · ${val.recommendation}`}
                  {val.size_mult !== undefined && ` · ${val.size_mult}x size`}
                </div>
              </div>
              <div className="mono" style={{ fontSize: '16px', fontWeight: 700, color }}>
                {winRate}%
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function MLInsights() {
  const { data } = useApi('/api/ml', 5000);

  if (!data) return <div style={{ color: 'var(--text-muted)' }}>Loading...</div>;

  return (
    <div>
      <div className="page-header">
        <h2>ML Insights</h2>
        <p>What the adaptive learner has discovered</p>
      </div>

      <div className="card mb-24">
        <div className="card-header"><span className="card-title">Score Weight Adjustments</span></div>
        <p style={{ fontSize: '12px', color: 'var(--text-muted)', marginBottom: '12px' }}>
          How the ML has shifted scoring weights from defaults (positive = increased importance)
        </p>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px' }}>
          {Object.entries(data.score_adjustments || {}).map(([k, v]) => (
            <AdjustmentRow key={k} factor={k} adjustment={v} />
          ))}
        </div>
      </div>

      <div className="grid-2 mb-24">
        <StatTable title="Regime Performance" stats={data.regime_stats || {}}
                   valueKey="win_rate" extraKey="recommendation" />
        <StatTable title="Session Performance" stats={data.session_stats || {}}
                   valueKey="win_rate" extraKey="aggression" />
      </div>

      <StatTable title="Pair Performance" stats={data.pair_stats || {}}
                 valueKey="win_rate" />
    </div>
  );
}
