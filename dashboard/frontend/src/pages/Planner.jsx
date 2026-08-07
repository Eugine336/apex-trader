import React, { useState, useMemo } from 'react';
import { useApi } from '../hooks/useApi';

const ACTION_BADGES = {
  ENTER: 'badge-green',
  WAIT: 'badge-yellow',
  SKIP: 'badge-muted',
};

function actionBadge(action) {
  return ACTION_BADGES[(action || '').toUpperCase()] || 'badge-muted';
}

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

function fmtTsSeconds(secs) {
  if (!secs) return '—';
  try {
    return fmtTs(new Date(secs * 1000).toISOString());
  } catch {
    return '—';
  }
}

function truncate(s, len) {
  if (!s) return '—';
  return s.length > len ? s.slice(0, len) + '…' : s;
}

function pnlColor(r) {
  if (r == null) return 'var(--text-secondary)';
  const n = Number(r);
  if (n > 0) return 'var(--green-bright)';
  if (n < 0) return 'var(--red-bright)';
  return 'var(--text-secondary)';
}

const STRATEGY_LABELS = {
  entry_mode_stats: 'Entry Mode',
  sl_strategy_stats: 'SL Strategy',
  tp_strategy_stats: 'TP Strategy',
};

function StrategyCard({ title, buckets }) {
  const entries = Object.entries(buckets || {}).sort((a, b) => (b[1]?.count || 0) - (a[1]?.count || 0));
  return (
    <div className="card">
      <div className="card-header"><span className="card-title">{title}</span></div>
      {entries.length === 0 ? (
        <div style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24, fontSize: 12 }}>No plans yet.</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {entries.map(([key, b]) => {
            const completed = b.completed || 0;
            const wr = completed ? Math.round((b.win_rate || 0) * 100) : null;
            return (
              <div key={key} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, fontWeight: 600, color: 'var(--text-secondary)', minWidth: 110 }}>
                  {key}
                </span>
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 12, fontWeight: 600, color: 'var(--text-primary)', minWidth: 36, textAlign: 'right' }}>
                  {b.count || 0}
                </span>
                <div style={{ flex: 1, height: 6, background: 'var(--bg-hover)', borderRadius: 3, overflow: 'hidden' }}>
                  <div style={{ height: '100%', width: `${wr == null ? 0 : wr}%`, borderRadius: 3, background: wr == null ? 'var(--text-muted)' : (wr >= 50 ? 'var(--green-bright)' : 'var(--red-bright)'), transition: 'width 0.3s' }} />
                </div>
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, fontWeight: 600, minWidth: 86, textAlign: 'right', color: 'var(--text-secondary)' }}>
                  {wr == null ? `${completed} done` : `${wr}% (${completed})`}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function Planner() {
  const { data, loading } = useApi('/api/planner?limit=100', 5000);
  const [symbolFilter, setSymbolFilter] = useState('');

  const plans = data?.plans || [];
  const stats = data?.stats || {};
  const calib = data?.calibration || {};
  const thresholds = calib.thresholds || {};

  const filtered = useMemo(() => {
    return plans.filter(p =>
      !symbolFilter || (p.symbol || '').toUpperCase().includes(symbolFilter.toUpperCase())
    );
  }, [plans, symbolFilter]);

  // Win rate across all completed planned trades.
  const completedRows = plans.filter(p => p.outcome && p.outcome.pnl_r != null);
  const wins = completedRows.filter(p => Number(p.outcome.pnl_r) > 0).length;
  const winRate = completedRows.length ? Math.round((wins / completedRows.length) * 100) : null;
  const avgR = completedRows.length
    ? (completedRows.reduce((s, p) => s + Number(p.outcome.pnl_r || 0), 0) / completedRows.length)
    : null;

  return (
    <div>
      <div className="page-header">
        <h2>Trade Planner</h2>
        <p>Plans coordinating every advisor, their outcomes, and self-calibration</p>
      </div>

      {/* Stats cards */}
      <div className="stat-grid mb-20" style={{ gridTemplateColumns: 'repeat(4, 1fr)' }}>
        <div className="stat-card">
          <div className="stat-label">Total Plans</div>
          <div className="stat-value">{stats.total_plans || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Completed</div>
          <div className="stat-value">{stats.total_completed || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Win Rate</div>
          <div className="stat-value" style={{ color: winRate == null ? 'var(--text-primary)' : (winRate >= 50 ? 'var(--green-bright)' : 'var(--red-bright)') }}>
            {winRate == null ? '—' : `${winRate}%`}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Avg R</div>
          <div className="stat-value" style={{ color: pnlColor(avgR) }}>
            {avgR == null ? '—' : (avgR >= 0 ? '+' : '') + avgR.toFixed(2)}
          </div>
        </div>
      </div>

      {/* Calibration status */}
      <div className="card mb-20">
        <div className="card-header"><span className="card-title">Self-Calibration Status</span></div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 16, marginBottom: 16 }}>
          <div>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', fontWeight: 600, marginBottom: 6 }}>Calibration</div>
            <span className={`badge ${calib.calibration_enabled ? 'badge-green' : 'badge-muted'}`}>
              {calib.calibration_enabled ? 'ENABLED' : 'DISABLED'}
            </span>
          </div>
          <div>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', fontWeight: 600, marginBottom: 6 }}>Completed Outcomes</div>
            <div style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 600, fontSize: 16, color: 'var(--text-primary)' }}>
              {calib.completed_outcomes || 0}
            </div>
          </div>
          <div>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', fontWeight: 600, marginBottom: 6 }}>Next Calibration At</div>
            <div style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 600, fontSize: 16, color: 'var(--text-primary)' }}>
              {calib.next_calibration_at || '—'}
              <span style={{ fontSize: 11, color: 'var(--text-muted)', marginLeft: 6 }}>
                ({calib.trades_until_next != null ? `${calib.trades_until_next} to go` : '—'})
              </span>
            </div>
          </div>
          <div>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', fontWeight: 600, marginBottom: 6 }}>Last Calibrated</div>
            <div style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 600, fontSize: 13, color: 'var(--text-secondary)' }}>
              {fmtTsSeconds(calib.last_calibrated_ts)}
            </div>
          </div>
        </div>
        {Object.keys(thresholds).length > 0 && (
          <div>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', fontWeight: 600, marginBottom: 8 }}>
              Live Planner Thresholds
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 10 }}>
              {Object.entries(thresholds).map(([k, v]) => (
                <div key={k} style={{ display: 'flex', justifyContent: 'space-between', gap: 8, padding: '4px 8px', background: 'var(--bg-hover)', borderRadius: 4 }}>
                  <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{k}</span>
                  <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, fontWeight: 600, color: 'var(--text-primary)' }}>
                    {typeof v === 'number' ? v : String(v)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Strategy distributions */}
      <div className="grid-2 mb-20" style={{ gridTemplateColumns: 'repeat(3, 1fr)' }}>
        <StrategyCard title={STRATEGY_LABELS.entry_mode_stats} buckets={stats.entry_mode_stats} />
        <StrategyCard title={STRATEGY_LABELS.sl_strategy_stats} buckets={stats.sl_strategy_stats} />
        <StrategyCard title={STRATEGY_LABELS.tp_strategy_stats} buckets={stats.tp_strategy_stats} />
      </div>

      {/* Filter */}
      <div className="filter-bar" style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 16, flexWrap: 'wrap' }}>
        <input
          type="text"
          placeholder="Filter by symbol…"
          value={symbolFilter}
          onChange={(e) => setSymbolFilter(e.target.value)}
          style={{
            marginLeft: 'auto', background: 'var(--bg-card)', border: '1px solid var(--border)',
            borderRadius: 6, color: 'var(--text-primary)', padding: '5px 12px', fontSize: 13, outline: 'none', width: 180,
          }}
        />
      </div>

      {/* Recent plans table */}
      <div className="card">
        <div className="card-header"><span className="card-title">Recent Trade Plans</span></div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 130 }}>Time</th>
                <th style={{ width: 90 }}>Symbol</th>
                <th style={{ width: 55 }}>Dir</th>
                <th style={{ width: 80 }}>Decision</th>
                <th style={{ width: 80 }}>Entry</th>
                <th className="right" style={{ width: 70 }}>Conv</th>
                <th className="right" style={{ width: 70 }}>Risk%</th>
                <th className="right" style={{ width: 60 }}>R</th>
                <th>Reasoning</th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 && (
                <tr><td colSpan={9} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 48 }}>
                  {loading ? 'Loading…' : 'No plans recorded yet — plans appear here as the bot evaluates setups.'}
                </td></tr>
              )}
              {filtered.map((p, i) => {
                const dir = p.direction || '';
                const oc = p.outcome || {};
                const r = oc.pnl_r;
                return (
                  <tr key={p.plan_id ? `${p.plan_id}-${i}` : i}>
                    <td style={{ fontSize: 11, color: 'var(--text-muted)' }}>{fmtTs(p.timestamp)}</td>
                    <td style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{p.symbol || '—'}</td>
                    <td className={dir === 'BUY' || dir === 'LONG' ? 'dir-long' : dir === 'SELL' || dir === 'SHORT' ? 'dir-short' : 'dir-neutral'}>
                      {dir || '—'}
                    </td>
                    <td><span className={`badge ${actionBadge(p.action)}`}>{p.action || '—'}</span></td>
                    <td style={{ fontSize: 11, color: 'var(--text-secondary)' }}>{p.entry_mode || '—'}</td>
                    <td className="right" style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 12 }}>
                      {p.confidence != null ? Number(p.confidence).toFixed(2) : '—'}
                    </td>
                    <td className="right" style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 12 }}>
                      {p.risk_pct != null ? Number(p.risk_pct).toFixed(2) : '—'}
                    </td>
                    <td className="right" style={{ color: pnlColor(r), fontFamily: "'JetBrains Mono', monospace", fontSize: 12, fontWeight: 600 }}>
                      {r != null ? (Number(r) >= 0 ? '+' : '') + Number(r).toFixed(2) : '—'}
                    </td>
                    <td className="text-col" style={{ fontSize: 11, color: 'var(--text-muted)' }}>{truncate(p.reasoning, 70)}</td>
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
