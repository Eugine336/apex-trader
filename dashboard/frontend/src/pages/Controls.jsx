import React, { useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import { useApi, postControl } from '../hooks/useApi';

const MODES = ['NORMAL', 'CAUTION', 'RECOVERY', 'FROZEN'];
const MODE_CLS = { NORMAL: 'btn-accent', CAUTION: 'btn-yellow', RECOVERY: 'btn-orange', FROZEN: 'btn-red' };
const RISK_BADGE_CLS = { NORMAL: 'risk-NORMAL', CAUTION: 'risk-CAUTION', RECOVERY: 'risk-RECOVERY', FROZEN: 'risk-FROZEN' };

function fmtUptime(sec) {
  if (!sec) return '0s';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

export default function Controls() {
  const { state } = useOutletContext();
  const { data: restData } = useApi('/api/status', 10000);
  const s = state.status || restData || {};

  const [feedback, setFeedback] = useState(null);
  const [confirmAction, setConfirmAction] = useState(null);

  const act = async (action, value = null) => {
    try {
      const res = await postControl(action, value);
      setFeedback({ type: 'success', msg: res.message || `Action "${action}" completed` });
    } catch (err) {
      setFeedback({ type: 'error', msg: err.message || 'Action failed' });
    }
    setTimeout(() => setFeedback(null), 4000);
  };

  const execConfirm = () => {
    if (confirmAction) act(confirmAction.action, confirmAction.value);
    setConfirmAction(null);
  };

  const botStatus = (s.bot_status || 'stopped').toLowerCase();
  const riskMode = s.risk_mode || 'NORMAL';

  return (
    <div>
      <div className="page-header">
        <h2>Controls</h2>
        <p>Bot management and emergency actions</p>
      </div>

      {feedback && (
        <div className={`toast toast-${feedback.type}`}>{feedback.msg}</div>
      )}

      <div className="grid-2 mb-20">
        <div className="card">
          <div className="card-header"><span className="card-title">Bot Status & Control</span></div>
          <div style={{ textAlign: 'center', marginBottom: 20 }}>
            <div style={{ display: 'inline-flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
              <span className={`status-dot ${botStatus}`} style={{ width: 14, height: 14 }} />
              <span style={{ fontSize: 24, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", textTransform: 'uppercase' }}>{botStatus}</span>
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>Uptime: {fmtUptime(s.uptime_seconds)}</div>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>Mode: {s.mode || 'N/A'}</div>
          </div>
          <div className="btn-group" style={{ justifyContent: 'center' }}>
            <button className="btn btn-green" disabled={botStatus === 'running'} onClick={() => act('start')}>▶ START</button>
            <button className="btn btn-yellow" disabled={botStatus !== 'running'} onClick={() => act('pause')}>⏸ PAUSE</button>
            <button className="btn btn-red" disabled={botStatus === 'stopped'} onClick={() => act('stop')}>⏹ STOP</button>
          </div>
        </div>

        <div className="card">
          <div className="card-header"><span className="card-title">Risk Mode Override</span></div>
          <div style={{ textAlign: 'center', marginBottom: 20 }}>
            <span className={`risk-badge ${RISK_BADGE_CLS[riskMode]}`} style={{ fontSize: 18, padding: '6px 20px' }}>{riskMode}</span>
          </div>
          <div className="btn-group" style={{ justifyContent: 'center', flexWrap: 'wrap' }}>
            {MODES.map((m) => (
              <button
                key={m}
                className={`btn ${MODE_CLS[m]} ${riskMode === m ? 'btn-active' : ''}`}
                onClick={() => act('risk_mode', m)}
              >
                {m}
              </button>
            ))}
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', textAlign: 'center', marginTop: 12 }}>
            Override lasts until next drawdown recalculation
          </div>
        </div>
      </div>

      <div className="card mb-20">
        <div className="card-header"><span className="card-title">System Info</span></div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 16 }}>
          <div>
            <div className="stat-label">MT5 Balance</div>
            <div className="mono" style={{ fontSize: 14 }}>${(s.mt5_balance || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}</div>
          </div>
          <div>
            <div className="stat-label">Deriv Balance</div>
            <div className="mono" style={{ fontSize: 14 }}>${(s.deriv_balance || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}</div>
          </div>
          <div>
            <div className="stat-label">Total Balance</div>
            <div className="mono" style={{ fontSize: 14 }}>${(s.account_balance || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}</div>
          </div>
          <div>
            <div className="stat-label">Total Instruments</div>
            <div className="mono" style={{ fontSize: 14 }}>66 (24/7: 24, 24/5: 42)</div>
          </div>
          <div>
            <div className="stat-label">Weekend Instruments</div>
            <div className="mono" style={{ fontSize: 14 }}>24</div>
          </div>
          <div>
            <div className="stat-label">Total Trades</div>
            <div className="mono" style={{ fontSize: 14 }}>{s.total_trades || 0}</div>
          </div>
          <div>
            <div className="stat-label">MT5 Connected</div>
            <div className="mono" style={{ fontSize: 14, color: s.mt5_connected ? 'var(--green-bright)' : 'var(--red-bright)' }}>{s.mt5_connected ? 'YES' : 'NO'}</div>
          </div>
          <div>
            <div className="stat-label">Deriv Connected</div>
            <div className="mono" style={{ fontSize: 14, color: s.deriv_connected ? 'var(--green-bright)' : 'var(--red-bright)' }}>{s.deriv_connected ? 'YES' : 'NO'}</div>
          </div>
          <div>
            <div className="stat-label">Trade Manager</div>
            <div className="mono" style={{ fontSize: 14 }}>{s.trade_manager_trades || 0} trades managed</div>
          </div>
        </div>
      </div>

      <div className="emergency-card">
        <div className="card-header"><span className="card-title" style={{ color: 'var(--red-bright)' }}>🚨 Emergency Actions</span></div>
        <div className="btn-group" style={{ justifyContent: 'center' }}>
          <button className="btn btn-red btn-lg" onClick={() => setConfirmAction({ action: 'close_all' })}>
            🚨 EMERGENCY CLOSE ALL POSITIONS
          </button>
          <button className="btn btn-red btn-lg" onClick={() => setConfirmAction({ action: 'risk_mode', value: 'FROZEN' })}>
            ⏹ HALT TRADING
          </button>
        </div>
      </div>

      {confirmAction && (
        <div className="confirm-overlay" onClick={() => setConfirmAction(null)}>
          <div className="confirm-dialog" onClick={(e) => e.stopPropagation()}>
            <h3>⚠ Confirm Action</h3>
            <p>
              {confirmAction.action === 'close_all'
                ? 'This will immediately close ALL open positions. This action cannot be undone.'
                : 'This will FREEZE all trading. No new positions will be opened.'}
            </p>
            <div className="btn-group">
              <button className="btn btn-red" onClick={execConfirm}>CONFIRM</button>
              <button className="btn" onClick={() => setConfirmAction(null)}>CANCEL</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
