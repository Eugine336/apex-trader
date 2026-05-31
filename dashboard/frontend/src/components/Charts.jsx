import React from 'react';
import {
  AreaChart, Area, BarChart, Bar, PieChart, Pie, Cell,
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Legend, ReferenceLine,
} from 'recharts';

const C = {
  green: '#22c55e', red: '#ef4444', accent: '#3b82f6',
  gold: '#f59e0b', muted: '#404060', grid: '#1e1e35',
  bg: '#111120', text: '#7878a0',
};

const ttStyle = { backgroundColor: '#161628', border: '1px solid #2a2a45', borderRadius: 4, fontSize: 12 };

export function EquityChart({ data }) {
  if (!data || !data.length) return <div className="skeleton skeleton-block" />;
  const d = data.map((p) => ({ date: p.date || p.time, equity: p.equity ?? p.value ?? 0 }));
  return (
    <ResponsiveContainer width="100%" height={260}>
      <AreaChart data={d}>
        <defs>
          <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor={C.green} stopOpacity={0.3} />
            <stop offset="95%" stopColor={C.green} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke={C.grid} strokeDasharray="3 3" />
        <XAxis dataKey="date" tick={{ fill: C.text, fontSize: 10 }} tickLine={false} />
        <YAxis tick={{ fill: C.text, fontSize: 10 }} tickLine={false} tickFormatter={(v) => `$${v.toLocaleString()}`} />
        <Tooltip contentStyle={ttStyle} formatter={(v) => [`$${Number(v).toLocaleString('en-US', { minimumFractionDigits: 2 })}`, 'Equity']} />
        <Area type="monotone" dataKey="equity" stroke={C.green} fill="url(#eqGrad)" strokeWidth={2} dot={false} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export function PnLBarChart({ data }) {
  if (!data || !data.length) return <div className="skeleton skeleton-block" />;
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={data}>
        <CartesianGrid stroke={C.grid} strokeDasharray="3 3" />
        <XAxis dataKey="date" tick={{ fill: C.text, fontSize: 10 }} tickLine={false} />
        <YAxis tick={{ fill: C.text, fontSize: 10 }} tickLine={false} tickFormatter={(v) => `$${v}`} />
        <Tooltip contentStyle={ttStyle} formatter={(v) => [`$${Number(v).toFixed(2)}`, 'P&L']} />
        <ReferenceLine y={0} stroke={C.muted} />
        <Bar dataKey="pnl" radius={[2, 2, 0, 0]}>
          {data.map((e, i) => (
            <Cell key={i} fill={(e.pnl || 0) >= 0 ? C.green : C.red} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export function WinRateDonut({ wins = 0, losses = 0 }) {
  const total = wins + losses;
  const rate = total > 0 ? ((wins / total) * 100).toFixed(1) : '0.0';
  const d = [
    { name: 'Wins', value: wins },
    { name: 'Losses', value: losses },
  ];
  return (
    <div style={{ position: 'relative', width: '100%', display: 'flex', justifyContent: 'center' }}>
      <ResponsiveContainer width="100%" height={220}>
        <PieChart>
          <Pie data={d} cx="50%" cy="50%" innerRadius={60} outerRadius={85} dataKey="value" stroke="none">
            <Cell fill={C.green} />
            <Cell fill={C.red} />
          </Pie>
          <Tooltip contentStyle={ttStyle} />
        </PieChart>
      </ResponsiveContainer>
      <div style={{ position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%, -50%)', textAlign: 'center' }}>
        <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 22, fontWeight: 700, color: '#e8e8f0' }}>{rate}%</div>
        <div style={{ fontSize: 10, color: C.text, textTransform: 'uppercase', letterSpacing: '0.5px' }}>Win Rate</div>
      </div>
    </div>
  );
}

export function MiniLine({ data, dataKey = 'value', color = C.accent, height = 40 }) {
  if (!data || !data.length) return null;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data}>
        <Line type="monotone" dataKey={dataKey} stroke={color} strokeWidth={1.5} dot={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}

export function FactorHeatmap({ instruments }) {
  if (!instruments || !instruments.length) return <div className="skeleton skeleton-block" />;
  const factors = ['structure', 'fvg', 'ob', 'liquidity', 'sweep', 'session', 'strength'];
  const top = [...instruments].sort((a, b) => (b.score || 0) - (a.score || 0)).slice(0, 20);

  const cellColor = (v) => {
    if (v >= 8) return 'rgba(22, 197, 94, 0.6)';
    if (v >= 5) return 'rgba(22, 197, 94, 0.3)';
    if (v >= 3) return 'rgba(22, 197, 94, 0.15)';
    return 'var(--bg-hover)';
  };

  return (
    <div>
      <div style={{ display: 'grid', gridTemplateColumns: '80px repeat(7, 1fr) 50px', gap: 2, marginBottom: 4 }}>
        <div className="heatmap-label" style={{ fontSize: 9, color: 'var(--text-muted)' }}>SYMBOL</div>
        {factors.map((f) => (
          <div key={f} className="heatmap-cell" style={{ fontSize: 9, color: 'var(--text-muted)', background: 'transparent', textTransform: 'uppercase' }}>{f}</div>
        ))}
        <div className="heatmap-cell" style={{ fontSize: 9, color: 'var(--text-muted)', background: 'transparent' }}>SCORE</div>
      </div>
      {top.map((inst) => (
        <div key={inst.symbol} style={{ display: 'grid', gridTemplateColumns: '80px repeat(7, 1fr) 50px', gap: 2, marginBottom: 2 }}>
          <div className="heatmap-label">{inst.symbol}</div>
          {factors.map((f) => {
            const v = inst.factors?.[f] ?? 0;
            return (
              <div key={f} className="heatmap-cell" style={{ background: cellColor(v), color: v >= 5 ? 'var(--green-bright)' : 'var(--text-muted)' }}>
                {v}
              </div>
            );
          })}
          <div className="heatmap-cell" style={{ background: 'transparent', color: 'var(--text-primary)', fontWeight: 700 }}>{inst.score}</div>
        </div>
      ))}
    </div>
  );
}

export function SessionBarChart({ data }) {
  if (!data || !data.length) return <div className="skeleton skeleton-block" />;
  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={data} layout="horizontal">
        <CartesianGrid stroke={C.grid} strokeDasharray="3 3" />
        <XAxis dataKey="name" tick={{ fill: C.text, fontSize: 10 }} tickLine={false} />
        <YAxis tick={{ fill: C.text, fontSize: 10 }} tickLine={false} tickFormatter={(v) => `${v}%`} />
        <Tooltip contentStyle={ttStyle} formatter={(v) => [`${Number(v).toFixed(1)}%`, 'Win Rate']} />
        <Bar dataKey="win_rate" radius={[2, 2, 0, 0]} fill={C.accent} />
      </BarChart>
    </ResponsiveContainer>
  );
}

export function HorizontalBarChart({ data, dataKey = 'trades', labelKey = 'name' }) {
  if (!data || !data.length) return <div className="skeleton skeleton-block" />;
  return (
    <ResponsiveContainer width="100%" height={Math.max(data.length * 30 + 40, 200)}>
      <BarChart data={data} layout="vertical">
        <CartesianGrid stroke={C.grid} strokeDasharray="3 3" />
        <XAxis type="number" tick={{ fill: C.text, fontSize: 10 }} tickLine={false} />
        <YAxis type="category" dataKey={labelKey} tick={{ fill: C.text, fontSize: 10 }} tickLine={false} width={70} />
        <Tooltip contentStyle={ttStyle} />
        <Bar dataKey={dataKey} radius={[0, 2, 2, 0]} fill={C.accent} />
      </BarChart>
    </ResponsiveContainer>
  );
}

export function CurrencyExposureChart({ data }) {
  if (!data) return <div className="skeleton skeleton-block" />;
  const d = Object.entries(data).map(([name, value]) => ({ name, value: Math.abs(value * 100) }));
  if (!d.length) return <div style={{ color: 'var(--text-muted)', fontSize: 12, textAlign: 'center', padding: 20 }}>No currency exposure data</div>;
  return (
    <ResponsiveContainer width="100%" height={Math.max(d.length * 32 + 40, 150)}>
      <BarChart data={d} layout="vertical">
        <CartesianGrid stroke={C.grid} strokeDasharray="3 3" />
        <XAxis type="number" tick={{ fill: C.text, fontSize: 10 }} tickLine={false} tickFormatter={(v) => `${v}%`} />
        <YAxis type="category" dataKey="name" tick={{ fill: C.text, fontSize: 10 }} tickLine={false} width={50} />
        <Tooltip contentStyle={ttStyle} formatter={(v) => [`${Number(v).toFixed(2)}%`, 'Exposure']} />
        <Bar dataKey="value" radius={[0, 2, 2, 0]} fill={C.accent} />
      </BarChart>
    </ResponsiveContainer>
  );
}

export function WeightAdjustmentChart({ data }) {
  if (!data) return <div className="skeleton skeleton-block" />;
  const d = Object.entries(data).map(([name, value]) => ({
    name: name.replace('_weight', '').replace('_', ' '),
    value,
  }));
  if (!d.length) return null;
  return (
    <ResponsiveContainer width="100%" height={Math.max(d.length * 36 + 40, 150)}>
      <BarChart data={d} layout="vertical">
        <CartesianGrid stroke={C.grid} strokeDasharray="3 3" />
        <XAxis type="number" tick={{ fill: C.text, fontSize: 10 }} tickLine={false} domain={['dataMin', 'dataMax']} />
        <YAxis type="category" dataKey="name" tick={{ fill: C.text, fontSize: 10 }} tickLine={false} width={80} />
        <ReferenceLine x={0} stroke={C.muted} />
        <Tooltip contentStyle={ttStyle} formatter={(v) => [Number(v).toFixed(1), 'Adjustment']} />
        <Bar dataKey="value" radius={[0, 2, 2, 0]}>
          {d.map((e, i) => (
            <Cell key={i} fill={e.value >= 0 ? C.green : C.red} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
