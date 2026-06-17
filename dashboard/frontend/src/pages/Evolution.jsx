import React from 'react';
import { useApi } from '../hooks/useApi';
import {
  ModuleInteractions,
  ParameterEvolution,
  SignalDiscovery,
  VirtualModules,
} from './Learning';

function StatTile({ label, value, sub, cls }) {
  return (
    <div className="stat-card">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${cls || 'neutral'}`}>{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

export default function Evolution() {
  const { data, loading } = useApi('/api/learning', 8000);

  if (loading && !data) {
    return (
      <div>
        <div className="page-header"><h2>Evolution (L5)</h2></div>
        <div className="skeleton skeleton-block" />
      </div>
    );
  }

  const vm = data?.virtual_modules || {};
  const counts = vm.counts || {};
  const sd = data?.signal_discovery || {};
  const pe = data?.param_evolution || {};
  const inter = data?.interactions || {};

  const activeVirtual = Number(counts.ACTIVE) || 0;
  const shadowVirtual = Number(counts.SHADOW) || 0;
  // Pending promotions = candidates earning their way to live: virtual shadow
  // modules + parameter-evolution shadows still validating.
  const pendingPromotions = shadowVirtual + (Number(pe.active_shadow_count) || 0);
  const toxicPairs = (inter.toxic_pairs || []).length;

  return (
    <div>
      <div className="page-header">
        <h2>Evolution (L5)</h2>
        <p>
          Self-evolution layer — parameter tournaments, module interaction
          discovery, synthetic signal mining, and virtual voting modules.
          Read-only; panels fill as trades close and candidates earn promotion.
        </p>
      </div>

      <div className="stats-grid" style={{ gridTemplateColumns: 'repeat(4, 1fr)' }}>
        <StatTile
          label="Active Virtual Modules"
          value={activeVirtual}
          sub={`${shadowVirtual} in shadow`}
          cls={activeVirtual > 0 ? 'positive' : 'neutral'}
        />
        <StatTile
          label="Signals in Shadow"
          value={shadowVirtual}
          sub={`${Number(sd.active_count) || 0} active rules`}
          cls="neutral"
        />
        <StatTile
          label="Pending Promotions"
          value={pendingPromotions}
          sub={`${Number(pe.active_shadow_count) || 0} param shadows`}
          cls="neutral"
        />
        <StatTile
          label="Toxic Pairs Detected"
          value={toxicPairs}
          sub={`${(inter.synergy_pairs || []).length} synergy pairs`}
          cls={toxicPairs > 0 ? 'negative' : 'neutral'}
        />
      </div>

      <ParameterEvolution d={data?.param_evolution} />
      <ModuleInteractions d={data?.interactions} />
      <SignalDiscovery d={data?.signal_discovery} />
      <VirtualModules d={data?.virtual_modules} />
    </div>
  );
}
