import React from 'react';

function fmtUptime(sec) {
  if (!sec) return '0s';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function fmtDollar(v) {
  if (v == null) return '$—';
  const abs = Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return v >= 0 ? `+$${abs}` : `-$${abs}`;
}

export default function StatusBar({ status, connected }) {
  const s = status || {};
  const botStatus = (s.bot_status || 'stopped').toLowerCase();
  const ddPct = s.max_daily_loss_pct ? ((s.daily_loss_pct || 0) / s.max_daily_loss_pct) * 100 : 0;
  const ddColor = ddPct > 80 ? 'var(--red-bright)' : ddPct > 50 ? 'var(--yellow-bright)' : 'var(--green-bright)';
  const dailyPnl = s.daily_pnl || 0;
  const dailyPct = s.account_balance ? ((dailyPnl / s.account_balance) * 100).toFixed(2) : '0.00';

  return (
    <div className="status-bar">
      <span className={`status-dot ${botStatus}`} />
      <span className="sb-val" style={{ textTransform: 'uppercase' }}>{botStatus}</span>
      <span className="sep">|</span>
      <span className="sb-label">Uptime:</span>
      <span className="sb-val">{fmtUptime(s.uptime_seconds)}</span>
      <span className="sep">|</span>
      <span className="sb-label">Balance:</span>
      <span className="sb-val">${(s.account_balance || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}</span>
      <span className="sep">|</span>
      <span className="sb-label">MT5:</span>
      <span className="sb-val">${(s.mt5_balance || 0).toLocaleString('en-US', { minimumFractionDigits: 0 })}</span>
      <span className="sb-label" style={{ marginLeft: 4 }}>Deriv:</span>
      <span className="sb-val">${(s.deriv_balance || 0).toLocaleString('en-US', { minimumFractionDigits: 0 })}</span>
      <span className="sep">|</span>
      <span className="sb-label">Today:</span>
      <span className={`sb-val ${dailyPnl >= 0 ? 'sb-pos' : 'sb-neg'}`}>
        {fmtDollar(dailyPnl)} ({dailyPnl >= 0 ? '+' : ''}{dailyPct}%)
      </span>
      <span className="sep">|</span>
      <span className="sb-label">Total P&L:</span>
      <span className={`sb-val ${(s.total_pnl || 0) >= 0 ? 'sb-pos' : 'sb-neg'}`}>
        {fmtDollar(s.total_pnl)}
      </span>
      <span className="sep">|</span>
      <span className="sb-label">Open:</span>
      <span className="sb-val">{s.open_trade_count || 0}/6</span>
      <span className="sep">|</span>
      <span className="sb-label">Win Rate:</span>
      <span className="sb-val sb-pos">{(s.win_rate || 0).toFixed(1)}%</span>
      <span className="sep">|</span>
      <span className="sb-label">Risk:</span>
      <span className={`risk-badge risk-${s.risk_mode || 'NORMAL'}`}>{s.risk_mode || 'NORMAL'}</span>
      <span className="sep">|</span>
      <span className="sb-label">DD:</span>
      <span className="dd-bar-wrap">
        <span className="sb-val">{(s.daily_loss_pct || 0).toFixed(1)}%</span>
        <span className="dd-bar">
          <span className="dd-bar-fill" style={{ width: `${Math.min(ddPct, 100)}%`, background: ddColor }} />
        </span>
        <span className="sb-val" style={{ color: 'var(--text-muted)' }}>/ {(s.max_daily_loss_pct || 3).toFixed(1)}%</span>
      </span>
    </div>
  );
}
