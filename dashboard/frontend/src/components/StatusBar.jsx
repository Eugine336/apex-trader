import React from 'react';
import { useApi } from '../hooks/useApi';

function formatUptime(seconds) {
  if (!seconds) return '0s';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

export default function StatusBar() {
  const { data } = useApi('/api/status', 3000);

  if (!data) return null;

  const statusClass = data.bot_status || 'stopped';

  return (
    <div className="status-bar">
      <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <span className={`status-dot ${statusClass}`} />
          <span style={{ fontWeight: 600, textTransform: 'uppercase', fontSize: '13px' }}>
            {data.bot_status}
          </span>
        </div>
        <span style={{ color: 'var(--text-muted)', fontSize: '12px' }}>
          Uptime: {formatUptime(data.uptime_seconds)}
        </span>
      </div>
      <div style={{ display: 'flex', gap: '24px', fontSize: '13px' }}>
        <span>
          Win Rate: <span className="mono" style={{ color: 'var(--green)', fontWeight: 600 }}>
            {data.win_rate}%
          </span>
        </span>
        <span>
          Today:{' '}
          <span className={`mono ${data.daily_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>
            ${data.daily_pnl?.toFixed(2)}
          </span>
        </span>
        <span>
          Open: <span className="mono" style={{ fontWeight: 600 }}>{data.open_trade_count}</span>
        </span>
        <span>
          Risk:{' '}
          <span className={`badge badge-${
            data.risk_mode === 'NORMAL' ? 'green' :
            data.risk_mode === 'CAUTION' ? 'yellow' :
            data.risk_mode === 'RECOVERY' ? 'red' : 'red'
          }`}>
            {data.risk_mode}
          </span>
        </span>
      </div>
    </div>
  );
}
