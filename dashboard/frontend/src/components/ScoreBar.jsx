import React from 'react';

export default function ScoreBar({ score, label }) {
  const cls = score >= 85 ? 'high' : score >= 50 ? 'medium' : 'low';
  const color = score >= 85 ? 'var(--green)' : score >= 50 ? 'var(--yellow)' : 'var(--red)';

  return (
    <div className="score-bar-wrap">
      {label && <span style={{ fontSize: '12px', color: 'var(--text-secondary)', minWidth: '60px' }}>{label}</span>}
      <div className="score-bar">
        <div className={`score-bar-fill ${cls}`} style={{ width: `${score}%` }} />
      </div>
      <span className="score-num" style={{ color }}>{score}</span>
    </div>
  );
}
