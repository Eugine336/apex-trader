import { getLearning } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import Panel from "../../components/engine/Panel";
import StatTile from "../../components/engine/StatTile";
import {
  CapitalAllocation,
  ExecutionProfiles,
  ModuleInteractions,
  ParameterEvolution,
  SignalDiscovery,
  VirtualModules,
} from "../../components/engine/learningPanels";
import { useEnginePoll } from "../../hooks/useEnginePoll";

export default function Evolution() {
  const { data, loading, error, unavailable } = useEnginePoll(getLearning, 8000);

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
    <div className="space-y-6">
      <EngineHeader
        title="Evolution (L5)"
        subtitle="Self-evolution layer — parameter tournaments, module interaction discovery, synthetic signal mining, and virtual voting modules. Read-only; panels fill as trades close and candidates earn promotion."
      />

      <EngineNotice
        loading={loading}
        unavailable={unavailable}
        error={error}
        hasData={!!data}
      />

      {data && (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <StatTile
              label="Active Virtual Modules"
              value={activeVirtual}
              accent={activeVirtual > 0 ? "text-emerald-400" : "text-gray-100"}
            />
            <StatTile label="Signals in Shadow" value={shadowVirtual} />
            <StatTile label="Pending Promotions" value={pendingPromotions} />
            <StatTile
              label="Toxic Pairs Detected"
              value={toxicPairs}
              accent={toxicPairs > 0 ? "text-red-400" : "text-gray-100"}
            />
          </div>

          <Panel
            title="Evolution summary"
            subtitle={`${shadowVirtual} virtual module(s) in shadow · ${Number(sd.active_count) || 0} active discovered rule(s) · ${Number(pe.active_shadow_count) || 0} parameter shadow(s) · ${(inter.synergy_pairs || []).length} synergy pair(s)`}
          >
            <p className="text-xs text-gray-500">
              Every candidate below earns its way to live: discovered rules and
              parameter changes prove themselves over shadow closes before they are
              ever recommended for promotion.
            </p>
          </Panel>

          <ParameterEvolution d={data.param_evolution} />
          <ModuleInteractions d={data.interactions} />
          <SignalDiscovery d={data.signal_discovery} />
          <VirtualModules d={data.virtual_modules} />
          <CapitalAllocation d={data.capital_allocation} />
          <ExecutionProfiles d={data.execution_profiles} />
        </>
      )}
    </div>
  );
}
