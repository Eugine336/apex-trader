import React from 'react';

export default function ScoreBar({ score = 0, label }) {
  const level = score >= 85 ? 'high' : score >= 50 ? 'medium' : 'low';
  return (
    <div className="score-bar-wrap">
      {label && <span style={{ fontSize: 10, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>{label}</span>}
      <div className="score-bar">
        <div className={`score-bar-fill ${level}`} style={{ width: `${Math.min(score, 100)}%` }} />
      </div>
      <span className="score-num">{score}</span>
    </div>
  );
}
