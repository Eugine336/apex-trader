import React from 'react';
import { useApi } from '../hooks/useApi';
import { EquityChart } from '../components/Charts';

// ── Shared helpers (match the conventions used across the dashboard) ──────────
function pct(x, dp = 0) {
  return `${((Number(x) || 0) * 100).toFixed(dp)}%`;
}

function tsAgo(ts) {
  if (!ts) return '—';
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

function StatusPill({ on, onLabel = 'OK', offLabel = 'OFF' }) {
  return (
    <span className={`badge ${on ? 'badge-green' : 'badge-muted'}`}>
      {on ? onLabel : offLabel}
    </span>
  );
}

// Drawdown / limit ratio → traffic-light colour (green < 50%, yellow 50-80%, red > 80%).
function limitColor(value, limit) {
  const v = Math.abs(Number(value) || 0);
  const l = Math.abs(Number(limit) || 0);
  if (l <= 0) return 'var(--text-primary)';
  const ratio = v / l;
  if (ratio > 0.8) return 'var(--red-bright)';
  if (ratio > 0.5) return 'var(--yellow-bright)';
  return 'var(--green-bright)';
}

function layerColor(state) {
  if (state === 'active') return 'var(--green-bright)';
  if (state === 'error') return 'var(--red-bright)';
  return 'var(--text-muted)';
}

function StatTile({ label, value, sub, color }) {
  return (
    <div className="stat-card">
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={color ? { color } : undefined}>{value}</div>
      {sub != null && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

// ── Health status bar ─────────────────────────────────────────────────────────
function HealthBar({ health, watchdog }) {
  const status = health?.status || 'ok';
  const broker = health?.broker || {};
  const statusOk = status === 'ok';
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">System Health</span>
        <span className={`badge ${statusOk ? 'badge-green' : 'badge-muted'}`}>
          {String(status).toUpperCase()}
        </span>
      </div>
      <div className="stats-grid" style={{ gridTemplateColumns: 'repeat(6, 1fr)', padding: '0 12px' }}>
        <StatTile
          label="Running"
          value={health?.running ? 'YES' : 'no'}
          color={health?.running ? 'var(--green-bright)' : 'var(--red-bright)'}
        />
        <StatTile
          label="Broker"
          value={broker.any_connected ? 'CONNECTED' : 'OFFLINE'}
          sub={`mt5 ${broker.mt5 ? 'on' : 'off'} · deriv ${broker.deriv ? 'on' : 'off'}`}
          color={broker.any_connected ? 'var(--green-bright)' : 'var(--red-bright)'}
        />
        <StatTile label="Open Positions" value={health?.open_positions ?? 0} />
        <StatTile
          label="Last Tick"
          value={health?.last_tick_age_seconds != null ? `${health.last_tick_age_seconds}s` : '—'}
          color={watchdog?.stalled ? 'var(--red-bright)' : 'var(--text-primary)'}
        />
        <StatTile label="Memory" value={health?.memory_mb != null ? `${health.memory_mb} MB` : '—'} />
        <StatTile
          label="Heartbeats"
          value={watchdog?.beats ?? 0}
          sub={watchdog?.stalled ? 'STALLED' : 'beating'}
          color={watchdog?.stalled ? 'var(--red-bright)' : 'var(--green-bright)'}
        />
      </div>
      {(health?.warnings || []).length > 0 && (
        <div style={{ padding: '8px 12px' }}>
          {health.warnings.map((w, i) => (
            <span key={i} className="badge badge-muted" style={{ marginRight: 6, color: 'var(--red-bright)' }}>
              ⚠ {w}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Drawdown + equity ──────────────────────────────────────────────────────────
function DrawdownPanel({ drawdown, equityCurve }) {
  const dd = drawdown || {};
  const state = dd.state || 'NORMAL';
  const stateOk = state === 'NORMAL' || state === '';
  const curve = (equityCurve || []).map((p) => ({
    date: p.ts ? new Date(Number(p.ts) * 1000).toLocaleTimeString() : '',
    equity: p.equity,
  }));
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">Drawdown &amp; Equity</span>
        <span className="badge" style={{ color: stateOk ? 'var(--green-bright)' : 'var(--red-bright)' }}>
          {state || 'NORMAL'}
        </span>
      </div>
      <div className="stats-grid" style={{ gridTemplateColumns: 'repeat(6, 1fr)', padding: '0 12px' }}>
        <StatTile
          label="Daily Drawdown"
          value={`${dd.current_daily_pct ?? 0}%`}
          sub={`limit ${dd.daily_limit ?? 0}%`}
          color={limitColor(dd.current_daily_pct, dd.daily_limit)}
        />
        <StatTile
          label="Rolling Drawdown"
          value={`${dd.current_rolling_pct ?? 0}%`}
          sub={`limit ${dd.rolling_limit ?? 0}%`}
          color={limitColor(dd.current_rolling_pct, dd.rolling_limit)}
        />
        <StatTile label="Hard Stop" value={`${dd.hard_stop_limit ?? 0}%`} />
        <StatTile
          label="Sizing Factor"
          value={dd.sizing_factor != null ? dd.sizing_factor : 1}
          color={Number(dd.sizing_factor) < 1 ? 'var(--yellow-bright)' : 'var(--text-primary)'}
        />
        <StatTile label="Peak Equity" value={dd.peak_equity ?? 0} />
        <StatTile
          label="Flatten?"
          value={dd.should_flatten ? 'YES' : 'no'}
          color={dd.should_flatten ? 'var(--red-bright)' : 'var(--text-primary)'}
        />
      </div>
      <div style={{ padding: '12px' }}>
        {curve.length > 0
          ? <EquityChart data={curve} />
          : <span className="text-muted">No equity history yet — fills as trades close.</span>}
      </div>
    </div>
  );
}

// ── Open positions ──────────────────────────────────────────────────────────────
function OpenPositions({ positions, regimeByPair }) {
  const rows = positions || [];
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">Open Positions</span>
        <span className="badge badge-muted">{rows.length}</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Instrument</th><th>Dir</th>
              <th className="right">Entry</th><th className="right">Current</th>
              <th className="right">P&amp;L (pips)</th><th className="right">P&amp;L ($)</th>
              <th className="right">SL</th><th className="right">TP1</th>
              <th>Regime</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={9} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>
                No open positions.
              </td></tr>
            )}
            {rows.map((t) => (
              <tr key={t.id}>
                <td style={{ fontWeight: 600 }}>{t.instrument}</td>
                <td style={{ color: t.direction === 'LONG' ? 'var(--green-bright)' : 'var(--red-bright)' }}>{t.direction}</td>
                <td className="right">{t.entry_price}</td>
                <td className="right">{t.current_price}</td>
                <td className="right" style={{ color: (t.pnl_pips || 0) >= 0 ? 'var(--green-bright)' : 'var(--red-bright)' }}>{t.pnl_pips}</td>
                <td className="right" style={{ color: (t.pnl_dollars || 0) >= 0 ? 'var(--green-bright)' : 'var(--red-bright)' }}>${t.pnl_dollars}</td>
                <td className="right">{t.stop_loss}</td>
                <td className="right">{t.tp1}</td>
                <td>{(regimeByPair && regimeByPair[t.instrument]) || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Risk events feed ──────────────────────────────────────────────────────────
function RiskEvents({ events }) {
  const rows = events || [];
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">Risk Events — Blocked Trades</span>
        <span className="badge badge-muted">{rows.length}</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Pair</th><th>Rule</th><th>Reason</th><th className="right">When</th></tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={4} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>
                No risk blocks recorded.
              </td></tr>
            )}
            {rows.slice(0, 30).map((e, i) => (
              <tr key={i}>
                <td>{e.pair}</td>
                <td>{e.rule}</td>
                <td>{e.reason}</td>
                <td className="right">{tsAgo(e.ts)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Regime map ──────────────────────────────────────────────────────────────────
function RegimeMap({ pairs }) {
  const rows = pairs || [];
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">Regime Map (per pair)</span>
        <span className="badge badge-muted">{rows.length}</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Pair</th><th>Regime</th><th className="right">Confidence</th><th className="right">Duration</th></tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={4} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>
                No regimes classified yet.
              </td></tr>
            )}
            {rows.slice(0, 40).map((p) => (
              <tr key={p.pair}>
                <td style={{ fontWeight: 600 }}>{p.pair}</td>
                <td>{p.regime}</td>
                <td className="right">{pct(p.confidence)}</td>
                <td className="right">{p.duration_sec ? `${Math.round(p.duration_sec)}s` : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Exposure breakdown ──────────────────────────────────────────────────────────
function Exposure({ exposure }) {
  const e = exposure || {};
  const perPair = e.per_pair || {};
  const perRegime = e.per_regime || {};
  return (
    <div className="card mb-20">
      <div className="card-header"><span className="card-title">Exposure Breakdown</span></div>
      <div className="stats-grid" style={{ gridTemplateColumns: 'repeat(4, 1fr)', padding: '0 12px' }}>
        <StatTile
          label="Positions"
          value={`${e.total_positions ?? 0}${e.max_positions ? ` / ${e.max_positions}` : ''}`}
        />
        <StatTile label="Long" value={e.long_positions ?? 0} color="var(--green-bright)" />
        <StatTile label="Short" value={e.short_positions ?? 0} color="var(--red-bright)" />
        <StatTile label="Directional Skew" value={`${e.directional_pct ?? 0}%`} />
      </div>
      <div className="grid-2" style={{ padding: '12px' }}>
        <div>
          <div className="stat-label" style={{ marginBottom: 6 }}>By Pair</div>
          {Object.keys(perPair).length === 0
            ? <span className="text-muted">—</span>
            : Object.entries(perPair).map(([k, v]) => (
                <span key={k} className="badge badge-muted" style={{ marginRight: 6, marginBottom: 4, display: 'inline-block' }}>
                  {k}: {v}
                </span>
              ))}
        </div>
        <div>
          <div className="stat-label" style={{ marginBottom: 6 }}>By Regime</div>
          {Object.keys(perRegime).length === 0
            ? <span className="text-muted">—</span>
            : Object.entries(perRegime).map(([k, v]) => (
                <span key={k} className="badge badge-muted" style={{ marginRight: 6, marginBottom: 4, display: 'inline-block' }}>
                  {k}: {v}
                </span>
              ))}
        </div>
      </div>
    </div>
  );
}

// ── Layer pulse ──────────────────────────────────────────────────────────────────
function LayerPulse({ layers }) {
  const entries = Object.entries(layers || {});
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">Adaptive Layer Pulse</span>
        <span className="badge badge-muted">{entries.length}</span>
      </div>
      <div className="stats-grid" style={{ gridTemplateColumns: 'repeat(4, 1fr)', padding: '0 12px 12px' }}>
        {entries.length === 0 && <span className="text-muted" style={{ padding: 12 }}>No adaptive layers reported.</span>}
        {entries.map(([name, info]) => (
          <div className="stat-card" key={name}>
            <div className="stat-label">{name}</div>
            <div className="stat-value" style={{ color: layerColor(info.state), fontSize: 16 }}>
              {String(info.state).toUpperCase()}
            </div>
            {info.store_mb > 0 && <div className="stat-sub">{info.store_mb} MB</div>}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Governor actions ──────────────────────────────────────────────────────────────
function GovernorActions({ actions }) {
  const rows = actions || [];
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">Module Governor Actions</span>
        <span className="badge badge-muted">{rows.length}</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Module</th><th>Transition</th><th>Reason</th><th className="right">When</th></tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={4} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>
                No governor actions yet.
              </td></tr>
            )}
            {rows.map((a, i) => (
              <tr key={i}>
                <td>{a.module}</td>
                <td>{a.from_state} → {a.to_state}</td>
                <td>{a.reason}</td>
                <td className="right">{tsAgo(a.ts)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Tick-latency profile (P4) ──────────────────────────────────────────────────────
function Performance({ perf }) {
  const p = perf || {};
  const tick = p.tick || {};
  const comps = p.components || [];
  const slow = p.slow_ticks || [];
  const recs = p.recommendations || [];
  const maxAvg = comps.reduce((m, c) => Math.max(m, Number(c.avg_ms) || 0), 0) || 1;
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">Tick Latency Profile (P4)</span>
        <StatusPill on={p.enabled} onLabel="LIVE" />
      </div>
      <div className="stats-grid" style={{ gridTemplateColumns: 'repeat(6, 1fr)', padding: '0 12px' }}>
        <StatTile label="Tick Avg" value={`${(tick.avg_ms || 0).toFixed(1)} ms`} />
        <StatTile label="Tick p50" value={`${(tick.p50_ms || 0).toFixed(1)} ms`} />
        <StatTile label="Tick p95" value={`${(tick.p95_ms || 0).toFixed(1)} ms`} />
        <StatTile label="Tick Max" value={`${(tick.max_ms || 0).toFixed(1)} ms`} />
        <StatTile label="Ticks" value={tick.tick_count ?? 0} />
        <StatTile
          label="Slow Ticks"
          value={tick.slow_tick_count ?? 0}
          sub={`> ${tick.slow_tick_threshold_ms ?? 0} ms`}
          color={(tick.slow_tick_count || 0) > 0 ? 'var(--yellow-bright)' : 'var(--green-bright)'}
        />
      </div>

      <div className="table-wrap" style={{ marginTop: 8 }}>
        <table>
          <thead>
            <tr>
              <th>Component</th>
              <th className="right">Avg</th><th className="right">p50</th>
              <th className="right">p95</th><th className="right">Max</th>
              <th className="right">Calls</th>
              <th style={{ width: '25%' }}>Share</th>
            </tr>
          </thead>
          <tbody>
            {comps.length === 0 && (
              <tr><td colSpan={7} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>
                No component timings yet — profile fills as ticks run.
              </td></tr>
            )}
            {comps.map((c) => (
              <tr key={c.component}>
                <td style={{ fontWeight: 600 }}>{c.component}</td>
                <td className="right">{(c.avg_ms || 0).toFixed(2)} ms</td>
                <td className="right">{(c.p50_ms || 0).toFixed(2)}</td>
                <td className="right">{(c.p95_ms || 0).toFixed(2)}</td>
                <td className="right">{(c.max_ms || 0).toFixed(2)}</td>
                <td className="right">{c.calls}</td>
                <td>
                  <div className="gauge-bar-outer" style={{ height: 8 }}>
                    <div
                      className="gauge-bar-fill"
                      style={{
                        width: `${Math.min((Number(c.avg_ms) || 0) / maxAvg * 100, 100)}%`,
                        background: 'var(--accent, var(--green-bright))',
                      }}
                    />
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {recs.length > 0 && (
        <div style={{ padding: '12px' }}>
          <div className="stat-label" style={{ marginBottom: 6 }}>Optimization Recommendations</div>
          {recs.map((r, i) => (
            <div
              key={i}
              className="badge"
              style={{
                display: 'block', marginBottom: 6, whiteSpace: 'normal', textAlign: 'left',
                color: r.severity === 'high' ? 'var(--red-bright)' : 'var(--yellow-bright)',
              }}
            >
              {r.suggestion}
            </div>
          ))}
        </div>
      )}

      {slow.length > 0 && (
        <div className="table-wrap" style={{ marginTop: 8 }}>
          <table>
            <thead>
              <tr><th>Slow Tick</th><th className="right">Total</th><th>Heaviest components</th><th className="right">When</th></tr>
            </thead>
            <tbody>
              {slow.map((s, i) => (
                <tr key={i}>
                  <td>#{slow.length - i}</td>
                  <td className="right" style={{ color: 'var(--yellow-bright)' }}>{(s.ms || 0).toFixed(1)} ms</td>
                  <td>{(s.components || []).map((c) => `${c.component} ${c.ms}ms`).join(', ')}</td>
                  <td className="right">{tsAgo(s.ts)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────────
export default function Operations() {
  const { data, loading } = useApi('/api/operations', 5000);

  if (loading && !data) {
    return (
      <div>
        <div className="page-header"><h2>Operations</h2></div>
        <div className="skeleton skeleton-block" />
      </div>
    );
  }

  const regimeByPair = {};
  (data?.regime_map || []).forEach((r) => { regimeByPair[r.pair] = r.regime; });

  return (
    <div>
      <div className="page-header">
        <h2>Operations</h2>
        <p>
          Control room — real-time system health, drawdown, exposure, regime,
          governor actions, and per-component tick latency. Read-only.
        </p>
      </div>

      <HealthBar health={data?.health} watchdog={data?.watchdog} />
      <DrawdownPanel drawdown={data?.drawdown} equityCurve={data?.equity_curve} />
      <OpenPositions positions={data?.open_positions} regimeByPair={regimeByPair} />
      <Exposure exposure={data?.exposure} />
      <RiskEvents events={data?.risk_events} />
      <RegimeMap pairs={data?.regime_map} />
      <LayerPulse layers={data?.layer_status} />
      <GovernorActions actions={data?.governor_actions} />
      <Performance perf={data?.performance} />
    </div>
  );
}
