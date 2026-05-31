import React from 'react';

export default function GaugeBar({ label, value = 0, max = 100, unit = '%', color, sub }) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
  const fillColor = color || (pct > 80 ? 'var(--red-bright)' : pct > 50 ? 'var(--yellow-bright)' : 'var(--green-bright)');

  return (
    <div className="gauge-card card">
      <div className="gauge-label">{label}</div>
      <div className="gauge-bar-outer">
        <div className="gauge-bar-fill" style={{ width: `${pct}%`, background: fillColor }} />
      </div>
      <div className="gauge-val" style={{ color: fillColor }}>
        {typeof value === 'number' ? value.toFixed(1) : value}{unit}
      </div>
      {sub && <div className="gauge-sub">{sub}</div>}
    </div>
  );
}
