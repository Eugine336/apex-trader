import React, { useState } from 'react';
import { useApi, postControl } from '../hooks/useApi';

export default function Controls() {
  const { data: status, refetch } = useApi('/api/status', 3000);
  const [feedback, setFeedback] = useState('');

  const act = async (action, value) => {
    const res = await postControl(action, value);
    setFeedback(res.message || res.error || 'Done');
    refetch();
    setTimeout(() => setFeedback(''), 3000);
  };

  if (!status) return <div style={{ color: 'var(--text-muted)' }}>Loading...</div>;

  return (
    <div>
      <div className="page-header">
        <h2>Controls</h2>
        <p>Start, stop, pause, and configure the trading bot</p>
      </div>

      {feedback && (
        <div style={{ padding: '10px 16px', background: 'var(--accent-glow)', border: '1px solid var(--accent)',
                       borderRadius: '8px', marginBottom: '16px', fontSize: '13px', color: 'var(--accent)' }}>
          {feedback}
        </div>
      )}

      <div className="grid-2 mb-24">
        <div className="card">
          <div className="card-header"><span className="card-title">Bot Control</span></div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
              <span className={`status-dot ${status.bot_status}`} />
              <span style={{ fontWeight: 600, fontSize: '16px', textTransform: 'uppercase' }}>
                {status.bot_status}
              </span>
            </div>
            <div className="btn-group">
              <button className="btn btn-green" onClick={() => act('start')}
                      disabled={status.bot_status === 'running'}>
                ▶ Start
              </button>
              <button className="btn btn-yellow" onClick={() => act('pause')}
                      disabled={status.bot_status !== 'running'}>
                ⏸ Pause
              </button>
              <button className="btn btn-red" onClick={() => act('stop')}
                      disabled={status.bot_status === 'stopped'}>
                ⏹ Stop
              </button>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-header"><span className="card-title">Risk Mode Override</span></div>
          <p style={{ fontSize: '12px', color: 'var(--text-muted)', marginBottom: '12px' }}>
            Current: <span className="badge badge-blue">{status.risk_mode}</span>
          </p>
          <div className="btn-group" style={{ flexWrap: 'wrap' }}>
            {['NORMAL', 'CAUTION', 'RECOVERY', 'FROZEN'].map((mode) => (
              <button key={mode}
                      className={`btn ${status.risk_mode === mode ? 'btn-accent' : ''}`}
                      onClick={() => act('risk_mode', mode)}>
                {mode}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <span className="card-title">Emergency Actions</span>
        </div>
        <p style={{ fontSize: '12px', color: 'var(--text-muted)', marginBottom: '16px' }}>
          These actions take effect immediately.
        </p>
        <button className="btn btn-red" style={{ fontWeight: 700 }}
                onClick={() => {
                  if (window.confirm('Close ALL open positions immediately?')) {
                    act('close_all');
                  }
                }}>
          🚨 Close All Positions
        </button>
      </div>
    </div>
  );
}
