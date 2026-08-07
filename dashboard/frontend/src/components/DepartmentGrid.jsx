import React from 'react';
import { useNavigate } from 'react-router-dom';

const STATUS_META = {
  active: { badge: 'badge-green', dot: 'on', label: 'ACTIVE' },
  degraded: { badge: 'badge-yellow', dot: 'warn', label: 'DEGRADED' },
  error: { badge: 'badge-red', dot: 'off', label: 'ERROR' },
  disabled: { badge: 'badge-orange', dot: 'off', label: 'DISABLED' },
  idle: { badge: 'badge-muted', dot: 'idle', label: 'IDLE' },
};

const DEPT_ICON = {
  intelligence: '🔬',
  consensus: '🧭',
  compliance: '🛡',
  portfolio: '💼',
  execution: '⚡',
  operations: '🩺',
  learning: '🧪',
  governance: '🏛',
  command: '🎛',
};

function DepartmentCard({ dept }) {
  const navigate = useNavigate();
  const meta = STATUS_META[dept.status] || STATUS_META.idle;
  return (
    <div
      className="dept-card"
      onClick={() => dept.route && navigate(dept.route)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if ((e.key === 'Enter' || e.key === ' ') && dept.route) navigate(dept.route);
      }}
    >
      <div className="dept-card-top">
        <span className="dept-icon">{DEPT_ICON[dept.key] || '⬡'}</span>
        <div className="dept-title-wrap">
          <div className="dept-name">
            <span className="dept-num">{dept.number}</span>
            {dept.name}
          </div>
          <div className="dept-mission">{dept.mission}</div>
        </div>
        <span className={`badge ${meta.badge} dept-badge`}>
          <span className={`dept-dot ${meta.dot}`} />
          {meta.label}
        </span>
      </div>
      <div className="dept-headline">{dept.headline || '—'}</div>
      {dept.metrics?.length > 0 && (
        <div className="dept-metrics">
          {dept.metrics.map((m) => (
            <div className="dept-metric" key={m.label}>
              <div className="dept-metric-val">{m.value}</div>
              <div className="dept-metric-lbl">{m.label}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function DepartmentGrid({ data }) {
  const departments = data?.departments || [];
  const flow = data?.signal_flow || [];

  if (departments.length === 0) {
    return (
      <div className="card mb-20">
        <div className="card-header"><span className="card-title">Organisation</span></div>
        <div style={{ color: 'var(--text-muted)', fontSize: 12, padding: 16 }}>
          Awaiting live system — department health will appear once attached.
        </div>
      </div>
    );
  }

  return (
    <div className="mb-20">
      {flow.length > 0 && (
        <div className="signal-flow">
          {flow.map((key, i) => {
            const d = departments.find((x) => x.key === key);
            const meta = STATUS_META[d?.status] || STATUS_META.idle;
            return (
              <React.Fragment key={key}>
                <div className={`flow-node ${meta.dot}`} title={d?.headline || key}>
                  <span className="flow-icon">{DEPT_ICON[key] || '⬡'}</span>
                  <span className="flow-label">{d?.name || key}</span>
                </div>
                {i < flow.length - 1 && <span className="flow-arrow">→</span>}
              </React.Fragment>
            );
          })}
        </div>
      )}
      <div className="dept-grid">
        {departments.map((d) => (
          <DepartmentCard key={d.key} dept={d} />
        ))}
      </div>
    </div>
  );
}
