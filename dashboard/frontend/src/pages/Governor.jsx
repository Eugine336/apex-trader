import React from 'react';
import { useApi } from '../hooks/useApi';

function fmtTs(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts);
    return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) + ' ' +
           d.toLocaleTimeString('en-GB', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch {
    return '—';
  }
}

const BLOCK_BADGES = {
  daily_loss_cap:        'badge-red',
  max_positions:         'badge-orange',
  currency_exposure:     'badge-yellow',
  sector_exposure:       'badge-yellow',
  correlated_positions:  'badge-purple',
};

function blockBadge(key) {
  return BLOCK_BADGES[(key || '').toLowerCase()] || 'badge-muted';
}

function ExposureTable({ title, data, max }) {
  const entries = Object.entries(data || {});
  return (
    <div className="card">
      <div className="card-header"><span className="card-title">{title}</span></div>
      {entries.length === 0 ? (
        <div style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24, fontSize: 12 }}>No open exposure.</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {entries.map(([key, count]) => {
            const pct = max > 0 ? Math.min(100, (count / max) * 100) : 0;
            const atLimit = count >= max;
            return (
              <div key={key} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)', minWidth: 70 }}>
                  {key}
                </span>
                <div style={{ flex: 1, height: 6, background: 'var(--bg-hover)', borderRadius: 3, overflow: 'hidden' }}>
                  <div style={{ height: '100%', width: `${pct}%`, borderRadius: 3, background: atLimit ? 'var(--red-bright)' : 'var(--accent-bright)', transition: 'width 0.3s' }} />
                </div>
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 12, fontWeight: 600, minWidth: 50, textAlign: 'right', color: atLimit ? 'var(--red-bright)' : 'var(--text-secondary)' }}>
                  {count}/{max}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function Governor() {
  const { data, loading } = useApi('/api/governor', 5000);
  const g = data || {};
  const halted = !!g.trading_halted;
  const dailyPct = Number(g.daily_pnl_pct || 0);
  const cap = Number(g.daily_loss_cap_pct || 3);
  // Bar fills toward the cap as losses mount (0% loss → empty, −cap% → full).
  const lossFill = Math.max(0, Math.min(100, (-dailyPct / cap) * 100));
  const blocks = g.recent_blocks || [];

  return (
    <div>
      <div className="page-header">
        <h2>Portfolio Governor</h2>
        <p>Portfolio-level risk limits — position caps, currency &amp; sector concentration, daily loss cap</p>
      </div>

      {/* Status cards */}
      <div className="stat-grid mb-20" style={{ gridTemplateColumns: 'repeat(4, 1fr)' }}>
        <div className="stat-card">
          <div className="stat-label">Trading Status</div>
          <div className="stat-value">
            <span className={`badge ${halted ? 'badge-red' : 'badge-green'}`}>
              {halted ? 'HALTED' : g.enabled ? 'ACTIVE' : 'OFF'}
            </span>
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Daily P&amp;L</div>
          <div className="stat-value" style={{ color: dailyPct > 0 ? 'var(--green-bright)' : dailyPct < 0 ? 'var(--red-bright)' : 'var(--text-primary)' }}>
            {(dailyPct >= 0 ? '+' : '') + dailyPct.toFixed(2)}%
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Open Positions</div>
          <div className="stat-value">{g.open_positions || 0}/{g.max_open_positions || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Recent Blocks</div>
          <div className="stat-value" style={{ color: blocks.length > 0 ? 'var(--yellow-bright)' : 'var(--text-primary)' }}>
            {blocks.length}
          </div>
        </div>
      </div>

      {/* Daily loss cap bar */}
      <div className="card mb-20">
        <div className="card-header">
          <span className="card-title">Daily Loss Cap</span>
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            cap −{cap.toFixed(1)}% · resume above −{Number(g.daily_loss_recovery_pct || 0).toFixed(1)}%
          </span>
        </div>
        <div className="gauge-bar-outer">
          <div
            className="gauge-bar-fill"
            style={{
              width: `${lossFill}%`,
              background: halted ? 'var(--red-bright)' : lossFill > 66 ? 'var(--yellow-bright)' : 'var(--green-bright)',
            }}
          />
        </div>
        <div style={{ marginTop: 8, fontSize: 12, color: 'var(--text-secondary)' }}>
          {halted
            ? `Trading halted — daily loss breached the −${cap.toFixed(1)}% cap. Entries resume once the day recovers above −${Number(g.daily_loss_recovery_pct || 0).toFixed(1)}%.`
            : `Daily P&L at ${(dailyPct >= 0 ? '+' : '') + dailyPct.toFixed(2)}% of balance.`}
        </div>
      </div>

      {/* Exposure tables */}
      <div className="grid-2 mb-20">
        <ExposureTable title="Currency Exposure" data={g.currency_exposure} max={g.max_currency_exposure || 0} />
        <ExposureTable title="Sector Exposure" data={g.sector_exposure} max={g.max_sector_exposure || 0} />
      </div>

      {/* Recent blocks */}
      <div className="card">
        <div className="card-header"><span className="card-title">Recent Governor Blocks</span></div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 140 }}>Time</th>
                <th style={{ width: 90 }}>Symbol</th>
                <th style={{ width: 55 }}>Dir</th>
                <th style={{ width: 160 }}>Blocked By</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {blocks.length === 0 && (
                <tr><td colSpan={5} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 48 }}>
                  {loading ? 'Loading…' : 'No governor blocks recorded — entries are within all portfolio limits.'}
                </td></tr>
              )}
              {blocks.map((b, i) => {
                const dir = b.direction || '';
                return (
                  <tr key={i}>
                    <td style={{ fontSize: 11, color: 'var(--text-muted)' }}>{fmtTs(b.timestamp)}</td>
                    <td style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{b.symbol || '—'}</td>
                    <td className={dir === 'BUY' || dir === 'LONG' ? 'dir-long' : dir === 'SELL' || dir === 'SHORT' ? 'dir-short' : 'dir-neutral'}>
                      {dir || '—'}
                    </td>
                    <td><span className={`badge ${blockBadge(b.blocked_by)}`}>{b.blocked_by || '—'}</span></td>
                    <td className="text-col" style={{ fontSize: 11, color: 'var(--text-muted)' }}>{b.reason || '—'}</td>
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
