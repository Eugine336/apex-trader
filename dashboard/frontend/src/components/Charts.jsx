import React from 'react';
import {
  LineChart, Line, BarChart, Bar, PieChart, Pie, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  AreaChart, Area,
} from 'recharts';

const COLORS = {
  green: '#22c55e',
  red: '#ef4444',
  accent: '#3b82f6',
  gold: '#f59e0b',
  muted: '#555570',
  grid: '#2a2a3a',
  bg: '#1a1a25',
};

const tooltipStyle = {
  contentStyle: {
    background: COLORS.bg,
    border: `1px solid ${COLORS.grid}`,
    borderRadius: '8px',
    fontSize: '12px',
    color: '#e4e4eb',
  },
};

export function EquityChart({ data }) {
  return (
    <ResponsiveContainer width="100%" height={250}>
      <AreaChart data={data}>
        <defs>
          <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor={COLORS.accent} stopOpacity={0.3} />
            <stop offset="95%" stopColor={COLORS.accent} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke={COLORS.grid} strokeDasharray="3 3" />
        <XAxis dataKey="date" tick={{ fontSize: 11, fill: COLORS.muted }} />
        <YAxis tick={{ fontSize: 11, fill: COLORS.muted }} />
        <Tooltip {...tooltipStyle} />
        <Area type="monotone" dataKey="equity" stroke={COLORS.accent}
              fill="url(#eqGrad)" strokeWidth={2} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export function PnLBarChart({ data }) {
  return (
    <ResponsiveContainer width="100%" height={250}>
      <BarChart data={data}>
        <CartesianGrid stroke={COLORS.grid} strokeDasharray="3 3" />
        <XAxis dataKey="date" tick={{ fontSize: 11, fill: COLORS.muted }} />
        <YAxis tick={{ fontSize: 11, fill: COLORS.muted }} />
        <Tooltip {...tooltipStyle} />
        <Bar dataKey="pnl" radius={[4, 4, 0, 0]}>
          {data.map((entry, i) => (
            <Cell key={i} fill={entry.pnl >= 0 ? COLORS.green : COLORS.red} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export function WinRateDonut({ wins, losses }) {
  const data = [
    { name: 'Wins', value: wins },
    { name: 'Losses', value: losses },
  ];
  const total = wins + losses;
  const rate = total > 0 ? ((wins / total) * 100).toFixed(1) : '0.0';

  return (
    <div style={{ position: 'relative', width: '200px', height: '200px', margin: '0 auto' }}>
      <ResponsiveContainer>
        <PieChart>
          <Pie data={data} cx="50%" cy="50%" innerRadius={60} outerRadius={85}
               paddingAngle={3} dataKey="value" strokeWidth={0}>
            <Cell fill={COLORS.green} />
            <Cell fill={COLORS.red} />
          </Pie>
        </PieChart>
      </ResponsiveContainer>
      <div style={{
        position: 'absolute', top: '50%', left: '50%',
        transform: 'translate(-50%, -50%)', textAlign: 'center',
      }}>
        <div style={{ fontSize: '28px', fontWeight: 700, fontFamily: 'JetBrains Mono',
                       color: COLORS.green }}>
          {rate}%
        </div>
        <div style={{ fontSize: '11px', color: COLORS.muted }}>WIN RATE</div>
      </div>
    </div>
  );
}

export function MiniLine({ data, dataKey, color = COLORS.accent, height = 60 }) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data}>
        <Line type="monotone" dataKey={dataKey} stroke={color}
              strokeWidth={2} dot={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}
