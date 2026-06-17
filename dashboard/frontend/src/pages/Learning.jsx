import React from 'react';
import { useApi } from '../hooks/useApi';

function pct(x) {
  return `${Math.round((Number(x) || 0) * 100)}%`;
}

function accColor(a) {
  const v = Number(a) || 0;
  if (v >= 0.55) return 'var(--green-bright)';
  if (v < 0.45) return 'var(--red-bright)';
  return 'var(--yellow-bright)';
}

function multColor(m) {
  const v = Number(m) || 0;
  if (v > 1.05) return 'var(--green-bright)';
  if (v < 0.95) return 'var(--red-bright)';
  return 'var(--text-primary)';
}

function StatusPill({ on, onLabel = 'LIVE', offLabel = 'OFF' }) {
  return (
    <span className={`badge ${on ? 'badge-green' : 'badge-muted'}`}>
      {on ? onLabel : offLabel}
    </span>
  );
}

function tsAgo(ts) {
  if (!ts) return '—';
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

// ── Signal Ledger ────────────────────────────────────────────────────────────
function SignalLedger({ d }) {
  const emitters = d?.emitters || [];
  const recent = d?.recent || [];
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">Signal Ledger</span>
        <StatusPill on={d?.enabled} />
      </div>
      <div className="grid-3 mb-16" style={{ padding: '0 12px' }}>
        <div className="stat-card">
          <div className="stat-label">Recorded</div>
          <div className="stat-value">{d?.total_recorded || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Graded</div>
          <div className="stat-value">{d?.total_graded || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Overall Accuracy</div>
          <div className="stat-value" style={{ color: accColor(d?.overall_accuracy) }}>
            {pct(d?.overall_accuracy)}
          </div>
        </div>
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', padding: '0 12px 10px' }}>
        Grading finalises at T+{d?.grading_delay_minutes || 0}m · checkpoints{' '}
        {(d?.check_intervals || []).map((i) => `T+${i}m`).join(', ') || '—'} · min move{' '}
        {d?.min_move_pct ?? 0}%
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Emitter</th>
              <th className="right">Graded</th>
              <th className="right">Accuracy</th>
              <th className="right">Traded</th>
              <th className="right">Blocked</th>
              <th className="right">Acc (blocked)</th>
            </tr>
          </thead>
          <tbody>
            {emitters.length === 0 && (
              <tr><td colSpan={6} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>
                No graded signals yet — accuracy appears as signals mature.
              </td></tr>
            )}
            {emitters.map((e) => (
              <tr key={e.emitter}>
                <td style={{ fontWeight: 600 }}>{e.emitter}</td>
                <td className="right">{e.total}</td>
                <td className="right" style={{ color: accColor(e.accuracy) }}>{pct(e.accuracy)}</td>
                <td className="right">{e.traded}</td>
                <td className="right">{e.blocked}</td>
                <td className="right" style={{ color: accColor(e.accuracy_blocked) }}>
                  {e.blocked ? pct(e.accuracy_blocked) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {recent.length > 0 && (
        <>
          <div className="card-header" style={{ marginTop: 8 }}>
            <span className="card-title">Recent graded signals</span>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Pair</th>
                  <th>Emitter</th>
                  <th>Dir</th>
                  <th className="right">Strength</th>
                  <th>State</th>
                  <th className="right">Correct</th>
                  <th className="right">MFE%</th>
                  <th className="right">MAE%</th>
                </tr>
              </thead>
              <tbody>
                {recent.map((r, i) => (
                  <tr key={i}>
                    <td>{r.pair}</td>
                    <td>{r.emitter}</td>
                    <td className={`dir-${(r.direction || '').toLowerCase()}`}>{r.direction}</td>
                    <td className="right">{Number(r.strength).toFixed(2)}</td>
                    <td>
                      {r.trade_opened
                        ? <span className="badge badge-blue">traded</span>
                        : r.gate_blocked_by
                          ? <span className="badge badge-orange">blocked: {r.gate_blocked_by}</span>
                          : <span className="badge badge-muted">no-trade</span>}
                    </td>
                    <td className="right">
                      {r.direction_correct === null || r.direction_correct === undefined
                        ? '—'
                        : r.direction_correct
                          ? <span style={{ color: 'var(--green-bright)' }}>✓</span>
                          : <span style={{ color: 'var(--red-bright)' }}>✗</span>}
                    </td>
                    <td className="right" style={{ color: 'var(--green-bright)' }}>{Number(r.max_favorable_pct).toFixed(2)}</td>
                    <td className="right" style={{ color: 'var(--red-bright)' }}>{Number(r.max_adverse_pct).toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

// ── Emitter Feedback ─────────────────────────────────────────────────────────
function EmitterFeedback({ d }) {
  const emitters = d?.emitters || [];
  const gates = d?.gates || [];
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">Emitter Feedback</span>
        <StatusPill on={d?.enabled} />
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Module</th>
              <th className="right">Signals</th>
              <th className="right">Acc (all)</th>
              <th className="right">Acc (traded)</th>
              <th className="right">Acc (blocked)</th>
              <th className="right">Value when blocked</th>
            </tr>
          </thead>
          <tbody>
            {emitters.length === 0 && (
              <tr><td colSpan={6} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>
                {d?.enabled ? 'No feedback yet — fills as signals are graded.' : 'Emitter feedback disabled.'}
              </td></tr>
            )}
            {emitters.map((e) => (
              <tr key={e.emitter}>
                <td style={{ fontWeight: 600 }}>{e.emitter}</td>
                <td className="right">{e.total_signals}</td>
                <td className="right" style={{ color: accColor(e.accuracy_all) }}>{pct(e.accuracy_all)}</td>
                <td className="right">{e.traded_signals ? pct(e.accuracy_traded) : '—'}</td>
                <td className="right">{e.blocked_signals ? pct(e.accuracy_blocked) : '—'}</td>
                <td className="right" style={{ color: accColor(e.signal_value_when_blocked) }}>
                  {e.blocked_signals ? pct(e.signal_value_when_blocked) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {gates.length > 0 && (
        <>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', padding: '6px 12px' }}>
            Gate effectiveness — high blocked accuracy means the gate is rejecting profitable signals.
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Gate</th>
                  <th className="right">Blocked</th>
                  <th className="right">Would-be correct</th>
                  <th className="right">Blocked accuracy</th>
                </tr>
              </thead>
              <tbody>
                {gates.map((g) => (
                  <tr key={g.gate}>
                    <td>{g.gate}</td>
                    <td className="right">{g.blocked}</td>
                    <td className="right">{g.would_have_been_correct}</td>
                    <td className="right" style={{ color: accColor(g.blocked_accuracy) }}>{pct(g.blocked_accuracy)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

// ── Vote Calibrator ──────────────────────────────────────────────────────────
function VoteCalibrator({ d }) {
  const modules = d?.modules || [];
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">Vote Calibrator</span>
        <StatusPill on={d?.enabled} />
      </div>
      <div className="grid-3 mb-16" style={{ padding: '0 12px' }}>
        <div className="stat-card">
          <div className="stat-label">Method</div>
          <div className="stat-value" style={{ fontSize: 18 }}>{d?.method || '—'}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Calibrated Modules</div>
          <div className="stat-value">{d?.calibrated_count || 0}/{d?.module_count || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Min Signals</div>
          <div className="stat-value">{d?.min_signals || 0}</div>
        </div>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Module</th>
              <th className="right">Weight ×</th>
              <th className="right">Samples</th>
              <th className="right">Accuracy</th>
              <th className="right">Status</th>
            </tr>
          </thead>
          <tbody>
            {modules.map((m) => (
              <tr key={m.module}>
                <td style={{ fontWeight: 600 }}>{m.module}</td>
                <td className="right" style={{ color: multColor(m.multiplier), fontFamily: "'JetBrains Mono', monospace" }}>
                  {Number(m.multiplier).toFixed(2)}×
                </td>
                <td className="right">{m.sample_size}</td>
                <td className="right">{m.sample_size ? pct(m.accuracy) : '—'}</td>
                <td className="right">
                  {m.calibrated
                    ? <span className="badge badge-green">calibrated</span>
                    : <span className="badge badge-muted">default 1.0×</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', padding: '6px 12px' }}>
        Multiplier &gt;1 amplifies a module's vote, &lt;1 dampens it. Modules below the min-signal
        threshold stay at a neutral 1.0× until enough graded signals accumulate.
      </div>
    </div>
  );
}

// ── Per-class Score Optimizer ────────────────────────────────────────────────
function ScoreOptimizer({ d }) {
  const classes = d?.classes || [];
  const global = d?.global_weights || {};
  const keys = Object.keys(global);
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">Per-Class Score Optimizer</span>
        <StatusPill on={d?.enabled} />
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', padding: '6px 12px' }}>
        {d?.enabled
          ? `Separate confluence weights per asset class — diverge from the global default after ${d?.min_trades_per_class || 0} class trades.`
          : 'Single global weight profile (per-class mode off).'}
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Class</th>
              <th>Status</th>
              {keys.map((k) => <th key={k} className="right">{k}</th>)}
            </tr>
          </thead>
          <tbody>
            <tr>
              <td style={{ fontWeight: 600 }}>global</td>
              <td><span className="badge badge-blue">prior</span></td>
              {keys.map((k) => <td key={k} className="right">{global[k]}</td>)}
            </tr>
            {classes.map((c) => (
              <tr key={c.asset_class}>
                <td style={{ fontWeight: 600 }}>{c.asset_class}</td>
                <td>
                  {c.diverged
                    ? <span className="badge badge-green">diverged</span>
                    : <span className="badge badge-muted">at prior</span>}
                </td>
                {keys.map((k) => (
                  <td key={k} className="right"
                      style={{ color: c.weights[k] !== global[k] ? 'var(--accent-bright)' : 'var(--text-primary)' }}>
                    {c.weights[k] ?? '—'}
                  </td>
                ))}
              </tr>
            ))}
            {classes.length === 0 && (
              <tr><td colSpan={keys.length + 2} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 20 }}>
                No per-class profiles yet — classes resolve to the global prior until they have enough trades.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Pair Learner ─────────────────────────────────────────────────────────────
function PairLearner({ d }) {
  const pairs = d?.pairs || [];
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">Pair Learner</span>
        <StatusPill on={d?.continuous_enabled} onLabel="CONTINUOUS" offLabel="BUCKETED" />
      </div>
      <div className="grid-3 mb-16" style={{ padding: '0 12px' }}>
        <div className="stat-card">
          <div className="stat-label">Learned Pairs</div>
          <div className="stat-value">{d?.pair_count || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Recommended</div>
          <div className="stat-value" style={{ color: 'var(--green-bright)' }}>{(d?.recommended || []).length}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Avoid</div>
          <div className="stat-value" style={{ color: 'var(--red-bright)' }}>{d?.avoid_count || 0}</div>
        </div>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Pair</th>
              <th className="right">Size ×</th>
              <th className="right">Win Rate</th>
              <th className="right">Trades</th>
              <th>Recommendation</th>
              <th className="right">Entry Acc</th>
              <th className="right">Mgmt Score</th>
              <th className="right">Opt SL (R)</th>
            </tr>
          </thead>
          <tbody>
            {pairs.length === 0 && (
              <tr><td colSpan={8} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>
                No learned pairs yet — profiles build as trades close.
              </td></tr>
            )}
            {pairs.map((p) => (
              <tr key={p.pair}>
                <td style={{ fontWeight: 600 }}>{p.pair}</td>
                <td className="right" style={{ color: multColor(p.multiplier), fontFamily: "'JetBrains Mono', monospace" }}>
                  {Number(p.multiplier).toFixed(2)}×
                </td>
                <td className="right" style={{ color: accColor(p.win_rate) }}>{pct(p.win_rate)}</td>
                <td className="right">{p.trades}</td>
                <td>
                  <span className={`badge ${
                    p.recommendation === 'AVOID' ? 'badge-red'
                      : p.recommendation === 'REDUCE_SIZE' ? 'badge-yellow'
                      : p.recommendation === 'TRADE' ? 'badge-green' : 'badge-muted'}`}>
                    {p.recommendation || '—'}
                  </span>
                </td>
                <td className="right">{p.entry_accuracy === null ? '—' : pct(p.entry_accuracy)}</td>
                <td className="right">{p.management_score === null ? '—' : Number(p.management_score).toFixed(2)}</td>
                <td className="right">{p.optimal_sl_r === null ? '—' : Number(p.optimal_sl_r).toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Tuner Agent ──────────────────────────────────────────────────────────────
function TunerAgent({ d }) {
  const tunables = d?.tunables || [];
  const audit = d?.audit || [];
  const missing = d?.unregistered_expected || [];
  const bypass = d?.bypass_attempts || [];
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">Tuner Agent</span>
        <StatusPill on={d?.enabled} onLabel="SOLE AUTHORITY" offLabel="OFF" />
      </div>
      <div className="grid-3 mb-16" style={{ padding: '0 12px' }}>
        <div className="stat-card">
          <div className="stat-label">Registered</div>
          <div className="stat-value">{d?.registered_count || 0}/{d?.expected_count || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Disabled (3-strike)</div>
          <div className="stat-value" style={{ color: (d?.disabled_tunables || []).length ? 'var(--red-bright)' : 'var(--text-primary)' }}>
            {(d?.disabled_tunables || []).length}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Bypass Attempts</div>
          <div className="stat-value" style={{ color: bypass.length ? 'var(--red-bright)' : 'var(--text-primary)' }}>
            {bypass.length}
          </div>
        </div>
      </div>
      {missing.length > 0 && (
        <div style={{ fontSize: 11, color: 'var(--yellow-bright)', padding: '0 12px 8px' }}>
          Unregistered expected: {missing.join(', ')}
        </div>
      )}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Tunable</th>
              <th>Frequency</th>
              <th className="right">Tunes</th>
              <th className="right">Last Tune</th>
              <th className="right">Failures</th>
              <th>State</th>
            </tr>
          </thead>
          <tbody>
            {tunables.length === 0 && (
              <tr><td colSpan={6} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>
                {d?.enabled ? 'No tunables registered.' : 'Tuner agent disabled — components self-tune.'}
              </td></tr>
            )}
            {tunables.map((t) => (
              <tr key={t.name}>
                <td style={{ fontWeight: 600 }}>{t.name}</td>
                <td style={{ color: 'var(--text-muted)' }}>{t.frequency}</td>
                <td className="right">{t.tune_count}</td>
                <td className="right">{tsAgo(t.last_tune_time)}</td>
                <td className="right" style={{ color: t.consecutive_failures ? 'var(--red-bright)' : 'var(--text-primary)' }}>
                  {t.consecutive_failures}
                </td>
                <td>
                  {t.disabled
                    ? <span className="badge badge-red">disabled</span>
                    : <span className="badge badge-green">active</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {audit.length > 0 && (
        <>
          <div className="card-header" style={{ marginTop: 8 }}>
            <span className="card-title">Recent tune results</span>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Tunable</th>
                  <th>Result</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {audit.map((a, i) => (
                  <tr key={i}>
                    <td>{tsAgo(a.timestamp)}</td>
                    <td>{a.tunable_name}</td>
                    <td>
                      {a.rollback_performed
                        ? <span className="badge badge-orange">rolled back</span>
                        : a.skipped
                          ? <span className="badge badge-muted">skipped</span>
                          : a.changed
                            ? <span className="badge badge-green">applied</span>
                            : a.success
                              ? <span className="badge badge-blue">no change</span>
                              : <span className="badge badge-red">failed</span>}
                    </td>
                    <td style={{ color: 'var(--text-muted)', fontSize: 11 }}>{a.error || a.reason || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

// ── Counterfactual Attribution (L4) ───────────────────────────────────────────
function CounterfactualAttribution({ d }) {
  const modules = d?.modules || [];
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">Counterfactual Attribution</span>
        <StatusPill on={d?.enabled} />
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', padding: '0 12px 10px' }}>
        Leave-one-out decision replay — each module's MARGINAL contribution to the
        trades taken. <b>Decisive</b> = trade only happened because of it; removing
        the module would delete those trades (and their R).
      </div>
      <div className="grid-3 mb-16" style={{ padding: '0 12px' }}>
        <div className="stat-card">
          <div className="stat-label">Modules Ranked</div>
          <div className="stat-value">{d?.module_count || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Trades Analyzed</div>
          <div className="stat-value">{d?.trades_analyzed || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Last Computed</div>
          <div className="stat-value">{tsAgo(d?.computed_at)}</div>
        </div>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Module</th>
              <th className="right">Involved</th>
              <th className="right">Decisive</th>
              <th className="right">Marginal R</th>
              <th className="right">Exp. (decisive)</th>
              <th className="right">Sharpe</th>
              <th className="right">Drawdown</th>
              <th>Better off without?</th>
            </tr>
          </thead>
          <tbody>
            {modules.length === 0 && (
              <tr><td colSpan={8} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>
                {d?.enabled
                  ? `No attribution computed yet — runs every ${d?.interval || 0} trades after ${d?.min_trades || 0} closed.`
                  : 'Counterfactual engine disabled.'}
              </td></tr>
            )}
            {modules.map((m) => (
              <tr key={m.module}>
                <td style={{ fontWeight: 600 }}>{m.module}</td>
                <td className="right">{m.trades_involved}</td>
                <td className="right">{m.decisive_trades}</td>
                <td className="right" style={{ color: multColor(1 + (Number(m.marginal_r) || 0)) }}>
                  {(Number(m.marginal_r) || 0).toFixed(2)}R
                </td>
                <td className="right">{(Number(m.expectancy_when_decisive) || 0).toFixed(2)}R</td>
                <td className="right">{(Number(m.sharpe_contribution) || 0).toFixed(2)}</td>
                <td className="right" style={{ color: 'var(--text-muted)' }}>
                  {(Number(m.drawdown_contribution) || 0).toFixed(2)}R
                </td>
                <td>
                  {m.better_off_without
                    ? <span className="badge badge-red">yes ({(Number(m.r_difference) || 0).toFixed(2)}R)</span>
                    : <span className="badge badge-green">no</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Module Interaction Discovery (L5b) ────────────────────────────────────────
function relColor(rel) {
  if (rel === 'SYNERGY') return 'var(--green-bright)';
  if (rel === 'TOXIC') return 'var(--red-bright)';
  return 'var(--text-muted)';
}

function ModuleInteractions({ d }) {
  const opt = d?.optimal_subset || {};
  const toxic = d?.toxic_pairs || [];
  const synergy = d?.synergy_pairs || [];
  const pairs = d?.pairs || [];
  const improvement = Number(opt.improvement) || 0;
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">Module Interactions (L5b)</span>
        <StatusPill on={d?.enabled} />
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', padding: '0 12px 10px' }}>
        Leave-K-out decision replay — non-additive interactions between modules.
        <b> Toxic</b> pairs reinforce each other's mistakes; <b>synergistic</b> pairs are
        worth more together than the sum of their parts. The <b>optimal subset</b> is the
        active-module configuration that would have produced the best book.
      </div>
      <div className="grid-3 mb-16" style={{ padding: '0 12px' }}>
        <div className="stat-card">
          <div className="stat-label">Modules Analyzed</div>
          <div className="stat-value">{d?.module_count || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Trades Analyzed</div>
          <div className="stat-value">{d?.trades_analyzed || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Last Computed</div>
          <div className="stat-value">{tsAgo(d?.computed_at)}</div>
        </div>
      </div>

      {/* Optimal active subset recommendation */}
      <div style={{ padding: '0 12px 12px' }}>
        <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>
          Optimal Active Subset
          {opt.search ? <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}> ({opt.search})</span> : null}
        </div>
        {(opt.active_modules || []).length === 0 ? (
          <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>
            {d?.enabled
              ? `No analysis computed yet — runs every ${d?.interval || 0} trades.`
              : 'Interaction discovery disabled.'}
          </div>
        ) : (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16, alignItems: 'center' }}>
            <div>
              {(opt.active_modules || []).map((m) => (
                <span key={m} className="badge badge-green" style={{ marginRight: 4 }}>{m}</span>
              ))}
              {(opt.shadow_modules || []).map((m) => (
                <span key={m} className="badge badge-red" style={{ marginRight: 4 }}>shadow: {m}</span>
              ))}
            </div>
            <div style={{ fontSize: 12 }}>
              Expected improvement:{' '}
              <b style={{ color: improvement > 0 ? 'var(--green-bright)' : 'var(--text-muted)' }}>
                {improvement > 0 ? '+' : ''}{improvement.toFixed(2)}R
              </b>
              <span style={{ color: 'var(--text-muted)' }}>
                {' '}(book {(Number(opt.total_r) || 0).toFixed(2)}R vs baseline {(Number(opt.baseline_r) || 0).toFixed(2)}R, {opt.trades_taken || 0} trades)
              </span>
            </div>
          </div>
        )}
      </div>

      {/* Toxic / synergy summary */}
      {(toxic.length > 0 || synergy.length > 0) && (
        <div style={{ padding: '0 12px 12px', fontSize: 12 }}>
          {toxic.length > 0 && (
            <div style={{ marginBottom: 4 }}>
              <span className="badge badge-red">toxic</span>{' '}
              {toxic.map((p) => (
                <span key={`${p.module_a}-${p.module_b}`} style={{ marginRight: 10 }}>
                  {p.module_a}+{p.module_b} ({(Number(p.interaction_effect) || 0).toFixed(2)}R)
                </span>
              ))}
            </div>
          )}
          {synergy.length > 0 && (
            <div>
              <span className="badge badge-green">synergy</span>{' '}
              {synergy.map((p) => (
                <span key={`${p.module_a}-${p.module_b}`} style={{ marginRight: 10 }}>
                  {p.module_a}+{p.module_b} (+{(Number(p.interaction_effect) || 0).toFixed(2)}R)
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Pairwise interaction matrix */}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Module A</th>
              <th>Module B</th>
              <th className="right">Δ remove A</th>
              <th className="right">Δ remove B</th>
              <th className="right">Δ remove A+B</th>
              <th className="right">Interaction</th>
              <th>Relationship</th>
            </tr>
          </thead>
          <tbody>
            {pairs.length === 0 && (
              <tr><td colSpan={7} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>
                {d?.enabled
                  ? 'No interaction matrix yet — needs at least 2 voting modules over recent trades.'
                  : 'Interaction discovery disabled.'}
              </td></tr>
            )}
            {pairs.map((p) => (
              <tr key={`${p.module_a}-${p.module_b}`}>
                <td style={{ fontWeight: 600 }}>{p.module_a}</td>
                <td style={{ fontWeight: 600 }}>{p.module_b}</td>
                <td className="right" style={{ color: 'var(--text-muted)' }}>{(Number(p.removal_delta_a) || 0).toFixed(2)}R</td>
                <td className="right" style={{ color: 'var(--text-muted)' }}>{(Number(p.removal_delta_b) || 0).toFixed(2)}R</td>
                <td className="right" style={{ color: 'var(--text-muted)' }}>{(Number(p.removal_delta_ab) || 0).toFixed(2)}R</td>
                <td className="right" style={{ color: relColor(p.relationship), fontWeight: 600 }}>
                  {(Number(p.interaction_effect) || 0).toFixed(2)}R
                </td>
                <td style={{ color: relColor(p.relationship) }}>{p.relationship}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ParameterEvolution({ d }) {
  const shadows = d?.active_shadows || [];
  const proms = d?.recent_promotions || [];
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">Parameter Evolution (L5a)</span>
        <StatusPill on={d?.enabled} />
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', padding: '0 12px 10px' }}>
        Explores consensus / ranker thresholds by replaying recent closed trades,
        walk-forward validates winners, then proves them over live closes (shadow)
        before <b>recommending</b> a promotion. It only proposes — applying a value
        is gated.
      </div>
      <div className="grid-3 mb-16" style={{ padding: '0 12px' }}>
        <div className="stat-card">
          <div className="stat-label">Active Shadows</div>
          <div className="stat-value">{d?.active_shadow_count || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Replay Lookback</div>
          <div className="stat-value">{d?.replay_lookback || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Shadow Trades</div>
          <div className="stat-value">{d?.shadow_validation_trades || 0}</div>
        </div>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Parameter</th>
              <th>State</th>
              <th className="right">Current</th>
              <th className="right">Proposed</th>
              <th className="right">Shadow Trades</th>
              <th className="right">Improvement /trade</th>
            </tr>
          </thead>
          <tbody>
            {shadows.length === 0 && (
              <tr><td colSpan={6} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>
                {d?.enabled ? 'No candidates in shadow validation yet.' : 'Parameter evolution disabled.'}
              </td></tr>
            )}
            {shadows.map((s, i) => (
              <tr key={`${s.param_name}-${i}`}>
                <td style={{ fontWeight: 600 }}>{s.param_name}</td>
                <td><span className="badge badge-blue">{s.state}</span></td>
                <td className="right">{Number(s.current_value).toFixed(3)}</td>
                <td className="right">{Number(s.proposed_value).toFixed(3)}</td>
                <td className="right">{s.shadow_trades}</td>
                <td className="right" style={{ color: multColor(1 + (Number(s.improvement_per_trade) || 0)) }}>
                  {(Number(s.improvement_per_trade) || 0).toFixed(3)}R
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {proms.length > 0 && (
        <>
          <div className="card-header" style={{ marginTop: 8 }}>
            <span className="card-title">Recent decisions</span>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Parameter</th>
                  <th>Decision</th>
                  <th className="right">Old</th>
                  <th className="right">New</th>
                  <th>When</th>
                </tr>
              </thead>
              <tbody>
                {proms.map((p, i) => (
                  <tr key={`${p.param_name}-${i}`}>
                    <td>{p.param_name}</td>
                    <td>
                      <span className={`badge ${p.decision === 'rejected' ? 'badge-red' : 'badge-green'}`}>
                        {p.decision}
                      </span>
                    </td>
                    <td className="right">{Number(p.old_value).toFixed(3)}</td>
                    <td className="right">{Number(p.new_value).toFixed(3)}</td>
                    <td style={{ color: 'var(--text-muted)' }}>{tsAgo(p.decided_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

function SignalDiscovery({ d }) {
  const rules = d?.rules || [];
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">Signal Discovery (L5c)</span>
        <StatusPill on={d?.enabled} />
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', padding: '0 12px 10px' }}>
        Mines module-condition <b>combinations</b> whose edge persists out-of-sample
        — rules nobody wrote, found in the data. Recommends candidates only; never
        auto-creates a live signal. Overfitting guards: Bonferroni α={(Number(d?.bonferroni_alpha) || 0).toFixed(3)},
        OOS retention ≥{Math.round((Number(d?.walk_forward_ratio_threshold) || 0) * 100)}%,
        active cap {d?.max_active_signals || 0}.
      </div>
      <div className="grid-3 mb-16" style={{ padding: '0 12px' }}>
        <div className="stat-card">
          <div className="stat-label">Rules</div>
          <div className="stat-value">{d?.rule_count || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Qualifying (OOS)</div>
          <div className="stat-value" style={{ color: 'var(--green-bright)' }}>{d?.qualifying_count || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Active / Cap</div>
          <div className="stat-value">{(d?.active_count || 0)} / {(d?.max_active_signals || 0)}</div>
        </div>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Rule</th>
              <th className="right">Support</th>
              <th className="right">Win%</th>
              <th className="right">Edge</th>
              <th className="right">Train edge</th>
              <th className="right">Test edge</th>
              <th className="right">OOS ret.</th>
              <th className="right">p</th>
              <th className="right">Score</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rules.length === 0 && (
              <tr><td colSpan={10} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>
                {d?.enabled
                  ? 'No rules discovered yet — needs more closed trades.'
                  : 'Signal discovery disabled.'}
              </td></tr>
            )}
            {rules.map((r, i) => (
              <tr key={`${r.label}-${i}`}>
                <td style={{ fontWeight: 600 }}>{r.label}</td>
                <td className="right">{r.support}</td>
                <td className="right" style={{ color: accColor(r.win_rate) }}>{pct(r.win_rate)}</td>
                <td className="right" style={{ color: multColor(1 + (Number(r.edge) || 0)) }}>
                  {(Number(r.edge) || 0).toFixed(2)}R
                </td>
                <td className="right">{(Number(r.train_edge) || 0).toFixed(2)}R</td>
                <td className="right">{(Number(r.test_edge) || 0).toFixed(2)}R</td>
                <td className="right" style={{ color: 'var(--text-muted)' }}>
                  {Math.round((Number(r.wf_ratio) || 0) * 100)}%
                </td>
                <td className="right" style={{ color: 'var(--text-muted)' }}>
                  {(Number(r.p_value) || 0).toFixed(3)}
                </td>
                <td className="right">{(Number(r.score) || 0).toFixed(2)}</td>
                <td>
                  {r.active
                    ? <span className="badge badge-green">active</span>
                    : r.qualifies
                      ? <span className="badge badge-blue">validated</span>
                      : <span className="badge badge-muted">candidate</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function modeBadge(mode) {
  if (mode === 'ACTIVE') return <span className="badge badge-green">active</span>;
  if (mode === 'SHADOW') return <span className="badge badge-blue">shadow</span>;
  return <span className="badge badge-muted">disabled</span>;
}

function VirtualModules({ d }) {
  const modules = d?.modules || [];
  const transitions = d?.transitions || [];
  const counts = d?.counts || {};
  const lastEval = d?.last_evaluation || null;
  return (
    <div className="card mb-20">
      <div className="card-header">
        <span className="card-title">Virtual Voting Modules (L5c)</span>
        <StatusPill on={d?.enabled} />
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', padding: '0 12px 10px' }}>
        Discovered rules promoted to <b>synthetic voting modules</b> that vote like a
        real module — but earn their way out of SHADOW first. Promotion gated:
        ≥{d?.shadow_trades_required || 0} shadow trades, accuracy ≥{pct(d?.min_shadow_accuracy)},
        active cap {d?.max_active || 0}. Kill switch:{' '}
        {d?.kill_switch ? 'on' : 'OFF (all weights 0)'}; promotion:{' '}
        {d?.promotion_enabled ? 'on' : 'off'}.
        {d?.restart_pending ? ` ${d.restart_pending} module(s) in restart-shadow.` : ''}
      </div>
      <div className="grid-3 mb-16" style={{ padding: '0 12px' }}>
        <div className="stat-card">
          <div className="stat-label">Active</div>
          <div className="stat-value" style={{ color: 'var(--green-bright)' }}>{counts.ACTIVE || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Shadow</div>
          <div className="stat-value">{counts.SHADOW || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Disabled</div>
          <div className="stat-value" style={{ color: 'var(--text-muted)' }}>{counts.DISABLED || 0}</div>
        </div>
      </div>
      {lastEval && (lastEval.promoted?.length || lastEval.retired?.length || lastEval.registered?.length) ? (
        <div style={{ fontSize: 11, color: 'var(--text-muted)', padding: '0 12px 8px' }}>
          Last pass — registered: {(lastEval.registered || []).length},
          promoted: {(lastEval.promoted || []).length},
          retired: {(lastEval.retired || []).length}.
        </div>
      ) : null}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Module</th>
              <th>Dir</th>
              <th>Mode</th>
              <th className="right">Weight</th>
              <th className="right">Live wt</th>
              <th className="right">Win%</th>
              <th className="right">Edge</th>
              <th>Source rule</th>
            </tr>
          </thead>
          <tbody>
            {modules.length === 0 && (
              <tr><td colSpan={8} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>
                {d?.enabled
                  ? 'No virtual modules yet — discovered rules register here as shadow candidates.'
                  : 'Virtual modules off (discovery engine not live).'}
              </td></tr>
            )}
            {modules.map((m, i) => (
              <tr key={`${m.name}-${i}`}>
                <td style={{ fontWeight: 600, fontSize: 11 }}>{m.name}</td>
                <td>{m.vote_direction}</td>
                <td>{modeBadge(m.mode)}{m.restart_shadow ? <span className="badge badge-muted" style={{ marginLeft: 4 }}>restart</span> : null}</td>
                <td className="right">{(Number(m.weight) || 0).toFixed(2)}</td>
                <td className="right" style={{ color: m.effective_weight > 0 ? 'var(--green-bright)' : 'var(--text-muted)' }}>
                  {(Number(m.effective_weight) || 0).toFixed(2)}
                </td>
                <td className="right" style={{ color: accColor(m.win_rate) }}>{pct(m.win_rate)}</td>
                <td className="right">{(Number(m.edge) || 0).toFixed(2)}R</td>
                <td style={{ fontSize: 10, color: 'var(--text-muted)' }}>{m.source_label}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {transitions.length > 0 && (
        <div className="table-wrap" style={{ marginTop: 8 }}>
          <table>
            <thead>
              <tr>
                <th>Module</th>
                <th>Transition</th>
                <th className="right">Weight</th>
                <th className="right">Acc</th>
                <th className="right">Marg R</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {transitions.map((t, i) => (
                <tr key={`${t.name}-${t.timestamp}-${i}`}>
                  <td style={{ fontSize: 11 }}>{t.name}</td>
                  <td>{t.old_mode} → {t.new_mode}</td>
                  <td className="right">{(Number(t.weight) || 0).toFixed(2)}</td>
                  <td className="right">{pct(t.accuracy)}</td>
                  <td className="right">{(Number(t.marginal_r) || 0).toFixed(3)}</td>
                  <td style={{ fontSize: 10, color: 'var(--text-muted)' }}>{t.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function Learning() {
  const { data, loading } = useApi('/api/learning', 8000);

  if (loading && !data) {
    return (
      <div>
        <div className="page-header"><h2>Learning Layer</h2></div>
        <div className="skeleton skeleton-block" />
      </div>
    );
  }

  return (
    <div>
      <div className="page-header">
        <h2>Learning Layer</h2>
        <p>How the system learns and tunes itself — every adaptive producer, made visible. Read-only; panels fill as signals are graded and trades close.</p>
      </div>

      <SignalLedger d={data?.signal_ledger} />
      <EmitterFeedback d={data?.emitter_feedback} />
      <VoteCalibrator d={data?.vote_calibrator} />
      <ScoreOptimizer d={data?.score_optimizer} />
      <PairLearner d={data?.pair_learner} />
      <CounterfactualAttribution d={data?.counterfactual} />
      <ModuleInteractions d={data?.interactions} />
      <ParameterEvolution d={data?.param_evolution} />
      <SignalDiscovery d={data?.signal_discovery} />
      <VirtualModules d={data?.virtual_modules} />
      <TunerAgent d={data?.tuner_agent} />
    </div>
  );
}
