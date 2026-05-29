import React from 'react';
import { useApi } from '../hooks/useApi';

function RiskModeIndicator({ mode }) {
  const config = {
    NORMAL: { color: 'var(--green)', label: 'Normal', risk: '2%', desc: 'Full trading power' },
    CAUTION: { color: 'var(--yellow)', label: 'Caution', risk: '1.5%', desc: 'Reduced after losses' },
    RECOVERY: { color: 'var(--red)', label: 'Recovery', risk: '1%', desc: 'Tight risk after drawdown' },
    FROZEN: { color: 'var(--red)', label: 'Frozen', risk: '0%', desc: 'Trading halted' },
  };
  const c = config[mode] || config.NORMAL;

  return (
    <div style={{ textAlign: 'center', padding: '20px' }}>
      <div style={{
        width: '80px', height: '80px', borderRadius: '50%',
        background: `${c.color}20`, border: `3px solid ${c.color}`,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        margin: '0 auto 12px', fontSize: '28px', fontWeight: 700,
        fontFamily: 'JetBrains Mono', color: c.color,
      }}>
        {c.risk}
      </div>
      <div style={{ fontWeight: 700, fontSize: '16px', color: c.color }}>{c.label}</div>
      <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: '4px' }}>{c.desc}</div>
    </div>
  );
}

export default function RiskMonitor() {
  const { data } = useApi('/api/risk', 3000);

  if (!data) return <div style={{ color: 'var(--text-muted)' }}>Loading...</div>;

  const dailyPct = Math.min((data.daily_loss_pct / data.max_daily_loss_pct) * 100, 100);
  const tradePct = (data.open_trade_count / data.max_open_trades) * 100;

  return (
    <div>
      <div className="page-header">
        <h2>Risk Monitor</h2>
        <p>Account protection and exposure tracking</p>
      </div>

      <div className="grid-3 mb-24">
        <div className="card">
          <div className="card-header"><span className="card-title">Risk Mode</span></div>
          <RiskModeIndicator mode={data.risk_mode} />
        </div>

        <div className="card">
          <div className="card-header"><span className="card-title">Daily Loss</span></div>
          <div className="gauge-wrap">
            <div style={{ display: 'flex', justifyContent: 'space-between', width: '100%', fontSize: '12px' }}>
              <span style={{ color: 'var(--text-muted)' }}>0%</span>
              <span className="mono" style={{ fontWeight: 600 }}>{data.daily_loss_pct?.toFixed(1)}%</span>
              <span style={{ color: 'var(--red)' }}>{data.max_daily_loss_pct}%</span>
            </div>
            <div className="gauge-bar">
              <div className="gauge-fill" style={{
                width: `${dailyPct}%`,
                background: dailyPct > 80 ? 'var(--red)' : dailyPct > 50 ? 'var(--yellow)' : 'var(--green)',
              }} />
            </div>
            <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
              {(data.max_daily_loss_pct - data.daily_loss_pct).toFixed(1)}% remaining before freeze
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-header"><span className="card-title">Open Positions</span></div>
          <div className="gauge-wrap">
            <div style={{ display: 'flex', justifyContent: 'space-between', width: '100%', fontSize: '12px' }}>
              <span style={{ color: 'var(--text-muted)' }}>0</span>
              <span className="mono" style={{ fontWeight: 600 }}>{data.open_trade_count}</span>
              <span style={{ color: 'var(--red)' }}>{data.max_open_trades}</span>
            </div>
            <div className="gauge-bar">
              <div className="gauge-fill" style={{
                width: `${tradePct}%`,
                background: tradePct > 80 ? 'var(--red)' : tradePct > 50 ? 'var(--yellow)' : 'var(--accent)',
              }} />
            </div>
          </div>
        </div>
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-header"><span className="card-title">Exposure</span></div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px', marginBottom: '6px' }}>
                <span>Account Exposure</span>
                <span className="mono" style={{ fontWeight: 600 }}>{data.exposure_pct?.toFixed(1)}%</span>
              </div>
              <div className="gauge-bar">
                <div className="gauge-fill" style={{
                  width: `${Math.min(data.exposure_pct * 10, 100)}%`,
                  background: 'var(--accent)',
                }} />
              </div>
            </div>
            <div style={{ fontSize: '13px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px solid var(--border)' }}>
                <span style={{ color: 'var(--text-muted)' }}>Balance</span>
                <span className="mono">${data.account_balance?.toLocaleString()}</span>
              </div>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-header"><span className="card-title">Streak</span></div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 12px', background: 'var(--green-bg)', borderRadius: '8px' }}>
              <span style={{ fontSize: '13px' }}>Consecutive Wins</span>
              <span className="mono" style={{ fontSize: '20px', fontWeight: 700, color: 'var(--green)' }}>
                {data.consecutive_wins}
              </span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 12px', background: 'var(--red-bg)', borderRadius: '8px' }}>
              <span style={{ fontSize: '13px' }}>Consecutive Losses</span>
              <span className="mono" style={{ fontSize: '20px', fontWeight: 700, color: 'var(--red)' }}>
                {data.consecutive_losses}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
