import React from 'react';
import { useOutletContext } from 'react-router-dom';
import { useApi } from '../hooks/useApi';
import GaugeBar from '../components/GaugeBar';
import { CurrencyExposureChart } from '../components/Charts';

const MODE_INFO = {
  NORMAL: { cls: 'risk-NORMAL', desc: 'Full power — 2% risk per trade', color: 'var(--accent-bright)' },
  CAUTION: { cls: 'risk-CAUTION', desc: 'Reduced — 1.5% risk per trade', color: 'var(--yellow-bright)' },
  RECOVERY: { cls: 'risk-RECOVERY', desc: 'Tight — 1% risk per trade', color: '#f97316' },
  FROZEN: { cls: 'risk-FROZEN', desc: 'HALTED — no new trades', color: 'var(--red-bright)' },
};

export default function RiskMonitor() {
  const { state } = useOutletContext();
  const { data: restData } = useApi('/api/risk', 5000);

  const r = state.risk || restData || {};
  const mode = r.risk_mode || r.mode || 'NORMAL';
  const mi = MODE_INFO[mode] || MODE_INFO.NORMAL;
  const ddPct = r.daily_loss_pct || 0;
  const maxDD = r.max_daily_loss_pct || 3;
  const balance = r.account_balance || 0;
  const remaining = ((maxDD - ddPct) / 100 * balance).toFixed(2);

  const eqBadge = (v) => {
    if (v === 'GOOD') return 'badge-green';
    if (v === 'ACCEPTABLE') return 'badge-yellow';
    return 'badge-red';
  };

  return (
    <div>
      <div className="page-header">
        <h2>Risk Monitor</h2>
        <p>Account protection and exposure</p>
      </div>

      <div className="card mb-20" style={{ textAlign: 'center', padding: 24 }}>
        <div style={{ display: 'inline-block', padding: '12px 32px', borderRadius: 6, border: `2px solid ${mi.color}`, background: `${mi.color}15` }}>
          <div style={{ fontSize: 28, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", color: mi.color }}>{mode}</div>
          <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>{mi.desc}</div>
          <div style={{ fontSize: 14, fontWeight: 600, fontFamily: "'JetBrains Mono', monospace", color: mi.color, marginTop: 8 }}>
            {(r.current_risk_pct || 0).toFixed(1)}% risk per trade
          </div>
        </div>
      </div>

      <div className="grid-3 mb-20">
        <GaugeBar
          label="Daily Drawdown"
          value={ddPct}
          max={maxDD}
          unit="%"
          sub={`$${remaining} remaining before freeze`}
        />
        <GaugeBar
          label="Open Positions"
          value={r.open_trade_count || 0}
          max={r.max_open_trades || 6}
          unit=""
          color="var(--accent-bright)"
          sub={`Max ${r.max_open_trades || 6} positions`}
        />
        <GaugeBar
          label="Account Exposure"
          value={r.exposure_pct || r.total_exposure_pct || 0}
          max={100}
          unit="%"
          sub={`Balance: $${balance.toLocaleString('en-US', { minimumFractionDigits: 2 })}`}
        />
      </div>

      <div className="grid-2 mb-20">
        <div className="card">
          <div className="card-header"><span className="card-title">Execution Quality</span></div>
          <div style={{ display: 'grid', gap: 12 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span className="stat-label" style={{ marginBottom: 0 }}>Quality</span>
              <span className={`badge ${eqBadge(r.execution_quality)}`}>{r.execution_quality || 'N/A'}</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span className="stat-label" style={{ marginBottom: 0 }}>Avg Slippage</span>
              <span className="mono" style={{ fontSize: 13 }}>{(r.avg_slippage_pips || 0).toFixed(1)} pips</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span className="stat-label" style={{ marginBottom: 0 }}>Avg Latency</span>
              <span className="mono" style={{ fontSize: 13 }}>{(r.avg_latency_ms || 0).toFixed(0)} ms</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span className="stat-label" style={{ marginBottom: 0 }}>Spread</span>
              <span className={`badge ${r.spread_is_wide ? 'badge-red' : 'badge-green'}`}>{r.spread_is_wide ? 'WIDE' : 'NORMAL'}</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span className="stat-label" style={{ marginBottom: 0 }}>Requotes</span>
              <span className="mono" style={{ fontSize: 13 }}>{r.requote_count || 0}</span>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-header"><span className="card-title">Risk Stats</span></div>
          <div style={{ display: 'grid', gap: 12 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span className="stat-label" style={{ marginBottom: 0 }}>Win Rate Today</span>
              <span className="mono pnl-positive" style={{ fontSize: 13 }}>{(r.win_rate_today || 0).toFixed(1)}%</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span className="stat-label" style={{ marginBottom: 0 }}>Profit Factor</span>
              <span className="mono" style={{ fontSize: 13 }}>{(r.profit_factor || 0).toFixed(2)}</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span className="stat-label" style={{ marginBottom: 0 }}>Max DD Today</span>
              <span className="mono pnl-negative" style={{ fontSize: 13 }}>{(r.max_drawdown_today || 0).toFixed(1)}%</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span className="stat-label" style={{ marginBottom: 0 }}>Consecutive Wins</span>
              <span className="mono pnl-positive" style={{ fontSize: 13 }}>{r.consecutive_wins || 0}</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span className="stat-label" style={{ marginBottom: 0 }}>Consecutive Losses</span>
              <span className="mono pnl-negative" style={{ fontSize: 13 }}>{r.consecutive_losses || 0}</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span className="stat-label" style={{ marginBottom: 0 }}>Score Threshold</span>
              <span className="mono" style={{ fontSize: 13 }}>{r.score_threshold || 0}</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span className="stat-label" style={{ marginBottom: 0 }}>Health</span>
              <span className={`badge ${r.health === 'HEALTHY' ? 'badge-green' : r.health === 'WARNING' ? 'badge-yellow' : 'badge-red'}`}>{r.health || 'N/A'}</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span className="stat-label" style={{ marginBottom: 0 }}>Daily P&L %</span>
              <span className={`mono ${(r.daily_pnl_pct || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'}`} style={{ fontSize: 13 }}>{(r.daily_pnl_pct || 0).toFixed(2)}%</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span className="stat-label" style={{ marginBottom: 0 }}>Weekly P&L %</span>
              <span className={`mono ${(r.weekly_pnl_pct || 0) >= 0 ? 'pnl-positive' : 'pnl-negative'}`} style={{ fontSize: 13 }}>{(r.weekly_pnl_pct || 0).toFixed(2)}%</span>
            </div>
          </div>
        </div>
      </div>

      <div className="card mb-20">
        <div className="card-header"><span className="card-title">Currency Exposure</span></div>
        <CurrencyExposureChart data={r.currency_exposures} />
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-header"><span className="card-title">⚠ Warnings</span></div>
          {(!r.warnings || r.warnings.length === 0) ? (
            <div style={{ color: 'var(--text-muted)', fontSize: 12, textAlign: 'center', padding: 16 }}>No active warnings</div>
          ) : (
            r.warnings.map((w, i) => (
              <div key={i} className="alert-row">
                <span className="alert-icon">⚠️</span>
                <span>{w}</span>
              </div>
            ))
          )}
        </div>
        <div className="card">
          <div className="card-header"><span className="card-title">📡 Spread Alerts</span></div>
          {(!r.spread_alerts || r.spread_alerts.length === 0) ? (
            <div style={{ color: 'var(--text-muted)', fontSize: 12, textAlign: 'center', padding: 16 }}>No spread alerts</div>
          ) : (
            r.spread_alerts.map((a, i) => (
              <div key={i} className="alert-row">
                <span className="alert-icon">📡</span>
                <span>{a}</span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
