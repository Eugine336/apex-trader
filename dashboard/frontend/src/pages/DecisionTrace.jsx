import React, { useState, useMemo } from 'react';
import { useApi } from '../hooks/useApi';

const STAGE_LABELS = {
  ranker: 'Ranker',
  correlation: 'Correlation',
  margin: 'Margin',
  max_trades: 'Max Trades',
  entry_engine: 'Entry Engine',
  decision_engine: 'Decision Engine',
  governor: 'Governor',
  planner: 'Planner',
  risk_stack: 'Risk Stack',
};

function stageLabel(s) {
  return STAGE_LABELS[s] || s;
}

function outcomeBadge(outcome) {
  if (!outcome) return 'badge-muted';
  if (outcome === 'TRADE_PLACED') return 'badge-green';
  if (outcome.startsWith('REJECTED')) return 'badge-red';
  if (outcome === 'ABORTED') return 'badge-yellow';
  return 'badge-muted';
}

function verdictColor(verdict) {
  const v = (verdict || '').toUpperCase();
  if (v.includes('BLOCK') || v.includes('SKIP') || v.includes('REJECT') || v.includes('VETO') || v.includes('WAIT')) {
    return 'var(--red-bright)';
  }
  if (v.includes('PASS') || v.includes('ENTER') || v.includes('LONG') || v.includes('SHORT') || v.includes('CHANGED')) {
    return 'var(--green-bright)';
  }
  return 'var(--text-secondary)';
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

function fmtEvidence(ev) {
  if (!ev || typeof ev !== 'object') return '';
  return Object.entries(ev)
    .filter(([, v]) => v !== null && v !== undefined && v !== '')
    .map(([k, v]) => `${k}=${typeof v === 'number' ? v : String(v)}`)
    .join('  ·  ');
}

function StageChain({ trace }) {
  const stages = trace.stages || [];
  const challengesByTarget = useMemo(() => {
    const m = {};
    (trace.challenges || []).forEach((c) => {
      (m[c.target_stage] = m[c.target_stage] || []).push(c);
    });
    return m;
  }, [trace]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '8px 4px' }}>
      {stages.length === 0 && (
        <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>No stage verdicts recorded.</div>
      )}
      {stages.map((s, i) => (
        <div key={i} style={{ borderLeft: `3px solid ${verdictColor(s.verdict)}`, paddingLeft: 10 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <span style={{ fontWeight: 700, fontSize: 12, minWidth: 130, color: 'var(--text-primary)' }}>
              {stageLabel(s.stage)}
            </span>
            <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, fontWeight: 700, color: verdictColor(s.verdict) }}>
              {s.verdict}
            </span>
            <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>by {s.owner}</span>
            <span style={{ fontSize: 10, color: 'var(--text-muted)', marginLeft: 'auto' }}>
              conf {Number(s.confidence ?? 0).toFixed(2)}
            </span>
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 2 }}>{s.justification}</div>
          {fmtEvidence(s.evidence) && (
            <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 10, color: 'var(--text-muted)', marginTop: 2 }}>
              {fmtEvidence(s.evidence)}
            </div>
          )}
          {(challengesByTarget[s.stage] || []).map((c, j) => (
            <div key={j} style={{ fontSize: 11, color: 'var(--yellow-bright)', marginTop: 3 }}>
              ⚑ challenged by <strong>{c.challenger}</strong>: {c.reason}
            </div>
          ))}
        </div>
      ))}
      {trace.final_reason && (
        <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>⇒ {trace.final_reason}</div>
      )}
    </div>
  );
}

export default function DecisionTrace() {
  const { data, loading } = useApi('/api/decision-trace?limit=150', 5000);
  const [symbolFilter, setSymbolFilter] = useState('');
  const [expanded, setExpanded] = useState(null);

  const traces = data?.traces || [];
  const stats = data?.stats || {};
  const funnel = stats.funnel || [];
  const rejections = stats.rejections || [];
  const challenges = stats.challenges || [];
  const confidence = stats.confidence || {};
  const maxFunnel = Math.max(1, ...funnel.map((f) => f.count));

  const filtered = useMemo(() => {
    if (!symbolFilter) return traces;
    const q = symbolFilter.toUpperCase();
    return traces.filter((t) => (t.pair || '').toUpperCase().includes(q));
  }, [traces, symbolFilter]);

  return (
    <div>
      <div className="page-header">
        <h2>Decision Trace</h2>
        <p>Pipeline awareness — every stage's justified verdict, challenges, and where opportunities die</p>
      </div>

      {/* Stat cards */}
      <div className="stat-grid mb-20" style={{ gridTemplateColumns: 'repeat(4, 1fr)' }}>
        <div className="stat-card">
          <div className="stat-label">Traces</div>
          <div className="stat-value">{stats.total_traces || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Trades Placed</div>
          <div className="stat-value" style={{ color: 'var(--green-bright)' }}>{stats.trades_placed || 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Challenges Raised</div>
          <div className="stat-value" style={{ color: (stats.challenge_count || 0) > 0 ? 'var(--yellow-bright)' : 'var(--text-primary)' }}>
            {stats.challenge_count || 0}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Rejecting Gates</div>
          <div className="stat-value">{rejections.length}</div>
        </div>
      </div>

      {/* Pipeline Funnel + Per-stage confidence */}
      <div className="grid-2 mb-20">
        <div className="card">
          <div className="card-header"><span className="card-title">Pipeline Funnel</span></div>
          {funnel.every((f) => f.count === 0) ? (
            <div style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24, fontSize: 12 }}>No traces recorded yet.</div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {funnel.map((f) => (
                <div key={f.stage} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ minWidth: 120, fontSize: 11, fontWeight: 600, color: 'var(--text-secondary)' }}>{stageLabel(f.stage)}</span>
                  <div style={{ flex: 1, height: 14, background: 'var(--bg-hover)', borderRadius: 3, overflow: 'hidden' }}>
                    <div style={{ height: '100%', width: `${(f.count / maxFunnel) * 100}%`, borderRadius: 3, background: 'var(--accent-bright)', transition: 'width 0.3s' }} />
                  </div>
                  <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 12, fontWeight: 600, minWidth: 44, textAlign: 'right', color: 'var(--text-secondary)' }}>
                    {f.count}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-header"><span className="card-title">Avg Confidence by Stage</span></div>
          {Object.keys(confidence).length === 0 ? (
            <div style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24, fontSize: 12 }}>No confidence data yet.</div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {Object.entries(confidence).map(([stage, v]) => (
                <div key={stage} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ minWidth: 120, fontSize: 11, fontWeight: 600, color: 'var(--text-secondary)' }}>{stageLabel(stage)}</span>
                  <div style={{ flex: 1, height: 14, background: 'var(--bg-hover)', borderRadius: 3, overflow: 'hidden' }}>
                    <div style={{ height: '100%', width: `${Math.max(0, Math.min(100, v * 100))}%`, borderRadius: 3, background: v > 0.6 ? 'var(--green-bright)' : v < 0.35 ? 'var(--red-bright)' : 'var(--yellow-bright)', transition: 'width 0.3s' }} />
                  </div>
                  <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 12, fontWeight: 600, minWidth: 44, textAlign: 'right', color: 'var(--text-secondary)' }}>
                    {Number(v).toFixed(2)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Rejection breakdown + Challenge feed */}
      <div className="grid-2 mb-20">
        <div className="card">
          <div className="card-header"><span className="card-title">Rejection Breakdown</span></div>
          {rejections.length === 0 ? (
            <div style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24, fontSize: 12 }}>No rejections recorded yet.</div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {rejections.map((r) => (
                <div key={r.stage}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span className="badge badge-red" style={{ minWidth: 120, textAlign: 'center' }}>{stageLabel(r.stage)}</span>
                    <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 12, fontWeight: 700, color: 'var(--red-bright)' }}>{r.count}</span>
                  </div>
                  {(r.reasons || []).slice(0, 3).map((reason, i) => (
                    <div key={i} style={{ fontSize: 11, color: 'var(--text-muted)', marginLeft: 8, marginTop: 2 }}>• {reason}</div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-header"><span className="card-title">Challenge Feed</span></div>
          {challenges.length === 0 ? (
            <div style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24, fontSize: 12 }}>No challenges raised — components agree.</div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxHeight: 280, overflowY: 'auto' }}>
              {challenges.map((c, i) => (
                <div key={i} style={{ borderLeft: '3px solid var(--yellow-bright)', paddingLeft: 10 }}>
                  <div style={{ fontSize: 12, color: 'var(--text-primary)' }}>
                    <strong>{c.challenger}</strong> ⚑ <strong>{stageLabel(c.target_stage)}</strong>
                    <span style={{ color: 'var(--text-muted)', marginLeft: 6 }}>{c.pair}</span>
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>{c.reason}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Filter */}
      <div className="filter-bar" style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 16 }}>
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

      {/* Per-pair trace history (expandable component-awareness view) */}
      <div className="card">
        <div className="card-header"><span className="card-title">Recent Traces — click to expand the full stage chain</span></div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 130 }}>Time</th>
                <th style={{ width: 90 }}>Symbol</th>
                <th style={{ width: 150 }}>Outcome</th>
                <th style={{ width: 120 }}>Died At</th>
                <th className="right" style={{ width: 70 }}>Stages</th>
                <th className="right" style={{ width: 80 }}>Challenges</th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 && (
                <tr><td colSpan={6} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 48 }}>
                  {loading ? 'Loading…' : 'No traces recorded yet — they appear here as the bot scans.'}
                </td></tr>
              )}
              {filtered.map((t, i) => {
                const key = t.trace_id || i;
                const isOpen = expanded === key;
                return (
                  <React.Fragment key={key}>
                    <tr style={{ cursor: 'pointer' }} onClick={() => setExpanded(isOpen ? null : key)}>
                      <td style={{ fontSize: 11, color: 'var(--text-muted)' }}>{fmtTs(t._event_ts)}</td>
                      <td style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{t.pair || '—'}</td>
                      <td><span className={`badge ${outcomeBadge(t.final_outcome)}`}>{t.final_outcome || '—'}</span></td>
                      <td style={{ fontSize: 11, color: 'var(--text-secondary)' }}>{t.rejected_at ? stageLabel(t.rejected_at) : '—'}</td>
                      <td className="right">{(t.stages || []).length}</td>
                      <td className="right" style={{ color: (t.challenges || []).length ? 'var(--yellow-bright)' : 'var(--text-secondary)' }}>
                        {(t.challenges || []).length}
                      </td>
                    </tr>
                    {isOpen && (
                      <tr>
                        <td colSpan={6} style={{ background: 'var(--bg-hover)' }}>
                          <StageChain trace={t} />
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
