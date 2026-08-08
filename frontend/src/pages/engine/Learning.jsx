import { getLearning } from "../../api/engine";
import { EngineHeader, EngineNotice } from "../../components/engine/EngineNotice";
import {
  BehaviorDiscovery,
  CapitalAllocation,
  CounterfactualAttribution,
  EmitterFeedback,
  ExecutionProfiles,
  ModuleInteractions,
  PairLearner,
  ParameterEvolution,
  RegimeDetection,
  RiskManagement,
  ScoreOptimizer,
  SignalDiscovery,
  SignalLedger,
  TunerAgent,
  VirtualModules,
  VoteCalibrator,
} from "../../components/engine/learningPanels";
import { useEnginePoll } from "../../hooks/useEnginePoll";

export default function Learning() {
  const { data, loading, error, unavailable } = useEnginePoll(getLearning, 8000);

  return (
    <div className="space-y-6">
      <EngineHeader
        title="Learning Layer"
        subtitle="How the system learns and tunes itself — every adaptive producer, made visible. Read-only; panels fill as signals are graded and trades close."
      />

      <EngineNotice
        loading={loading}
        unavailable={unavailable}
        error={error}
        hasData={!!data}
      />

      {data && (
        <>
          <SignalLedger d={data.signal_ledger} />
          <EmitterFeedback d={data.emitter_feedback} />
          <VoteCalibrator d={data.vote_calibrator} />
          <ScoreOptimizer d={data.score_optimizer} />
          <PairLearner d={data.pair_learner} />
          <CounterfactualAttribution d={data.counterfactual} />
          <ModuleInteractions d={data.interactions} />
          <ParameterEvolution d={data.param_evolution} />
          <SignalDiscovery d={data.signal_discovery} />
          <VirtualModules d={data.virtual_modules} />
          <CapitalAllocation d={data.capital_allocation} />
          <ExecutionProfiles d={data.execution_profiles} />
          <RegimeDetection d={data.regime_detection} />
          <RiskManagement d={data.risk_management} />
          <BehaviorDiscovery d={data.behavior_discovery} />
          <TunerAgent d={data.tuner_agent} />
        </>
      )}
    </div>
  );
}
