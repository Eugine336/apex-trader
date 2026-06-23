// Shared Learning + Governance sub-panels, ported from the original dashboard's
// Learning.jsx. Each panel takes a `d` prop holding its slice of the
// /api/engine/learning payload. Learning.jsx renders all 16; Evolution.jsx
// reuses the self-evolution subset (the same composition the original used).
import { badgeClass } from "../../utils/engineFormat";

// ── shared helpers ───────────────────────────────────────────────────────────
export function pct(x) {
  return `${Math.round((Number(x) || 0) * 100)}%`;
}

export function accColor(a) {
  const v = Number(a) || 0;
  if (v >= 0.55) return "text-emerald-400";
  if (v < 0.45) return "text-red-400";
  return "text-yellow-400";
}

export function multColor(m) {
  const v = Number(m) || 0;
  if (v > 1.05) return "text-emerald-400";
  if (v < 0.95) return "text-red-400";
  return "text-gray-100";
}

export function tsAgo(ts) {
  if (!ts) return "—";
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

export function StatusPill({ on, onLabel = "LIVE", offLabel = "OFF" }) {
  return (
    <span className={badgeClass(on ? "green" : "muted")}>
      {on ? onLabel : offLabel}
    </span>
  );
}

// A learning sub-panel card: title + status pill header, optional note, body.
function LCard({ title, on, onLabel, offLabel, note, children }) {
  return (
    <div className="rounded-lg border border-gray-700 bg-gray-800 p-5 shadow-lg">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-100">{title}</h2>
        {on !== undefined && (
          <StatusPill on={on} onLabel={onLabel} offLabel={offLabel} />
        )}
      </div>
      {note && <p className="mb-3 text-xs text-gray-500">{note}</p>}
      {children}
    </div>
  );
}

function MiniStat({ label, value, accent = "text-gray-100" }) {
  return (
    <div className="rounded-md border border-gray-700 bg-gray-900/40 p-3">
      <div className="text-[11px] uppercase tracking-wider text-gray-500">
        {label}
      </div>
      <div className={`mt-0.5 text-lg font-semibold ${accent}`}>{value}</div>
    </div>
  );
}

const TH =
  "py-2 pr-2 text-left text-xs uppercase text-gray-500 font-medium";
const THR = `${TH} text-right`;

function Empty({ cols, children }) {
  return (
    <tr>
      <td colSpan={cols} className="py-6 text-center text-xs text-gray-500">
        {children}
      </td>
    </tr>
  );
}

// ── 1. Signal Ledger ─────────────────────────────────────────────────────────
export function SignalLedger({ d }) {
  const emitters = d?.emitters || [];
  const recent = d?.recent || [];
  return (
    <LCard title="Signal Ledger" on={d?.enabled}>
      <div className="mb-4 grid grid-cols-3 gap-3">
        <MiniStat label="Recorded" value={d?.total_recorded || 0} />
        <MiniStat label="Graded" value={d?.total_graded || 0} />
        <MiniStat
          label="Overall Accuracy"
          value={pct(d?.overall_accuracy)}
          accent={accColor(d?.overall_accuracy)}
        />
      </div>
      <p className="mb-3 text-xs text-gray-500">
        Grading finalises at T+{d?.grading_delay_minutes || 0}m · checkpoints{" "}
        {(d?.check_intervals || []).map((i) => `T+${i}m`).join(", ") || "—"} ·
        min move {d?.min_move_pct ?? 0}%
      </p>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[520px] text-sm">
          <thead>
            <tr className="border-b border-gray-700">
              <th className={TH}>Emitter</th>
              <th className={THR}>Graded</th>
              <th className={THR}>Accuracy</th>
              <th className={THR}>Traded</th>
              <th className={THR}>Blocked</th>
              <th className={THR}>Acc (blocked)</th>
            </tr>
          </thead>
          <tbody>
            {emitters.length === 0 && (
              <Empty cols={6}>
                No graded signals yet — accuracy appears as signals mature.
              </Empty>
            )}
            {emitters.map((e) => (
              <tr key={e.emitter} className="border-b border-gray-800">
                <td className="py-2 pr-2 font-semibold text-gray-100">
                  {e.emitter}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">{e.total}</td>
                <td className={`py-2 pr-2 text-right ${accColor(e.accuracy)}`}>
                  {pct(e.accuracy)}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">{e.traded}</td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {e.blocked}
                </td>
                <td
                  className={`py-2 pr-2 text-right ${accColor(e.accuracy_blocked)}`}
                >
                  {e.blocked ? pct(e.accuracy_blocked) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {recent.length > 0 && (
        <div className="mt-4 overflow-x-auto">
          <p className="mb-2 text-xs font-semibold text-gray-300">
            Recent graded signals
          </p>
          <table className="w-full min-w-[640px] text-sm">
            <thead>
              <tr className="border-b border-gray-700">
                <th className={TH}>Pair</th>
                <th className={TH}>Emitter</th>
                <th className={TH}>Dir</th>
                <th className={THR}>Strength</th>
                <th className={TH}>State</th>
                <th className={THR}>Correct</th>
                <th className={THR}>MFE%</th>
                <th className={THR}>MAE%</th>
              </tr>
            </thead>
            <tbody>
              {recent.map((r, i) => (
                <tr key={i} className="border-b border-gray-800">
                  <td className="py-2 pr-2 text-gray-200">{r.pair}</td>
                  <td className="py-2 pr-2 text-gray-200">{r.emitter}</td>
                  <td
                    className={`py-2 pr-2 ${
                      (r.direction || "").toUpperCase() === "LONG"
                        ? "text-emerald-400"
                        : (r.direction || "").toUpperCase() === "SHORT"
                          ? "text-red-400"
                          : "text-gray-400"
                    }`}
                  >
                    {r.direction}
                  </td>
                  <td className="py-2 pr-2 text-right font-mono text-gray-200">
                    {Number(r.strength).toFixed(2)}
                  </td>
                  <td className="py-2 pr-2">
                    {r.trade_opened ? (
                      <span className={badgeClass("blue")}>traded</span>
                    ) : r.gate_blocked_by ? (
                      <span className={badgeClass("orange")}>
                        blocked: {r.gate_blocked_by}
                      </span>
                    ) : (
                      <span className={badgeClass("muted")}>no-trade</span>
                    )}
                  </td>
                  <td className="py-2 pr-2 text-right">
                    {r.direction_correct === null ||
                    r.direction_correct === undefined ? (
                      "—"
                    ) : r.direction_correct ? (
                      <span className="text-emerald-400">✓</span>
                    ) : (
                      <span className="text-red-400">✗</span>
                    )}
                  </td>
                  <td className="py-2 pr-2 text-right text-emerald-400">
                    {Number(r.max_favorable_pct).toFixed(2)}
                  </td>
                  <td className="py-2 pr-2 text-right text-red-400">
                    {Number(r.max_adverse_pct).toFixed(2)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </LCard>
  );
}

// ── 2. Emitter Feedback ──────────────────────────────────────────────────────
export function EmitterFeedback({ d }) {
  const emitters = d?.emitters || [];
  const gates = d?.gates || [];
  return (
    <LCard title="Emitter Feedback" on={d?.enabled}>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[560px] text-sm">
          <thead>
            <tr className="border-b border-gray-700">
              <th className={TH}>Module</th>
              <th className={THR}>Signals</th>
              <th className={THR}>Acc (all)</th>
              <th className={THR}>Acc (traded)</th>
              <th className={THR}>Acc (blocked)</th>
              <th className={THR}>Value when blocked</th>
            </tr>
          </thead>
          <tbody>
            {emitters.length === 0 && (
              <Empty cols={6}>
                {d?.enabled
                  ? "No feedback yet — fills as signals are graded."
                  : "Emitter feedback disabled."}
              </Empty>
            )}
            {emitters.map((e) => (
              <tr key={e.emitter} className="border-b border-gray-800">
                <td className="py-2 pr-2 font-semibold text-gray-100">
                  {e.emitter}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {e.total_signals}
                </td>
                <td className={`py-2 pr-2 text-right ${accColor(e.accuracy_all)}`}>
                  {pct(e.accuracy_all)}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {e.traded_signals ? pct(e.accuracy_traded) : "—"}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {e.blocked_signals ? pct(e.accuracy_blocked) : "—"}
                </td>
                <td
                  className={`py-2 pr-2 text-right ${accColor(
                    e.signal_value_when_blocked,
                  )}`}
                >
                  {e.blocked_signals ? pct(e.signal_value_when_blocked) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {gates.length > 0 && (
        <div className="mt-4 overflow-x-auto">
          <p className="mb-2 text-xs text-gray-500">
            Gate effectiveness — high blocked accuracy means the gate is rejecting
            profitable signals.
          </p>
          <table className="w-full min-w-[420px] text-sm">
            <thead>
              <tr className="border-b border-gray-700">
                <th className={TH}>Gate</th>
                <th className={THR}>Blocked</th>
                <th className={THR}>Would-be correct</th>
                <th className={THR}>Blocked accuracy</th>
              </tr>
            </thead>
            <tbody>
              {gates.map((g) => (
                <tr key={g.gate} className="border-b border-gray-800">
                  <td className="py-2 pr-2 text-gray-200">{g.gate}</td>
                  <td className="py-2 pr-2 text-right text-gray-200">
                    {g.blocked}
                  </td>
                  <td className="py-2 pr-2 text-right text-gray-200">
                    {g.would_have_been_correct}
                  </td>
                  <td
                    className={`py-2 pr-2 text-right ${accColor(g.blocked_accuracy)}`}
                  >
                    {pct(g.blocked_accuracy)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </LCard>
  );
}

// ── 3. Vote Calibrator ───────────────────────────────────────────────────────
export function VoteCalibrator({ d }) {
  const modules = d?.modules || [];
  return (
    <LCard title="Vote Calibrator" on={d?.enabled}>
      <div className="mb-4 grid grid-cols-3 gap-3">
        <MiniStat label="Method" value={d?.method || "—"} />
        <MiniStat
          label="Calibrated Modules"
          value={`${d?.calibrated_count || 0}/${d?.module_count || 0}`}
        />
        <MiniStat label="Min Signals" value={d?.min_signals || 0} />
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[480px] text-sm">
          <thead>
            <tr className="border-b border-gray-700">
              <th className={TH}>Module</th>
              <th className={THR}>Weight ×</th>
              <th className={THR}>Samples</th>
              <th className={THR}>Accuracy</th>
              <th className={THR}>Status</th>
            </tr>
          </thead>
          <tbody>
            {modules.length === 0 && <Empty cols={5}>No calibrated modules yet.</Empty>}
            {modules.map((m) => (
              <tr key={m.module} className="border-b border-gray-800">
                <td className="py-2 pr-2 font-semibold text-gray-100">
                  {m.module}
                </td>
                <td
                  className={`py-2 pr-2 text-right font-mono ${multColor(
                    m.multiplier,
                  )}`}
                >
                  {Number(m.multiplier).toFixed(2)}×
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {m.sample_size}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {m.sample_size ? pct(m.accuracy) : "—"}
                </td>
                <td className="py-2 pr-2 text-right">
                  {m.calibrated ? (
                    <span className={badgeClass("green")}>calibrated</span>
                  ) : (
                    <span className={badgeClass("muted")}>default 1.0×</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-3 text-xs text-gray-500">
        Multiplier &gt;1 amplifies a module's vote, &lt;1 dampens it. Modules below
        the min-signal threshold stay at a neutral 1.0× until enough graded signals
        accumulate.
      </p>
    </LCard>
  );
}

// ── 4. Per-class Score Optimizer ─────────────────────────────────────────────
export function ScoreOptimizer({ d }) {
  const classes = d?.classes || [];
  const global = d?.global_weights || {};
  const keys = Object.keys(global);
  return (
    <LCard
      title="Per-Class Score Optimizer"
      on={d?.enabled}
      note={
        d?.enabled
          ? `Separate confluence weights per asset class — diverge from the global default after ${d?.min_trades_per_class || 0} class trades.`
          : "Single global weight profile (per-class mode off)."
      }
    >
      <div className="overflow-x-auto">
        <table className="w-full min-w-[480px] text-sm">
          <thead>
            <tr className="border-b border-gray-700">
              <th className={TH}>Class</th>
              <th className={TH}>Status</th>
              {keys.map((k) => (
                <th key={k} className={THR}>
                  {k}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr className="border-b border-gray-800">
              <td className="py-2 pr-2 font-semibold text-gray-100">global</td>
              <td className="py-2 pr-2">
                <span className={badgeClass("blue")}>prior</span>
              </td>
              {keys.map((k) => (
                <td key={k} className="py-2 pr-2 text-right text-gray-200">
                  {global[k]}
                </td>
              ))}
            </tr>
            {classes.map((c) => (
              <tr key={c.asset_class} className="border-b border-gray-800">
                <td className="py-2 pr-2 font-semibold text-gray-100">
                  {c.asset_class}
                </td>
                <td className="py-2 pr-2">
                  {c.diverged ? (
                    <span className={badgeClass("green")}>diverged</span>
                  ) : (
                    <span className={badgeClass("muted")}>at prior</span>
                  )}
                </td>
                {keys.map((k) => (
                  <td
                    key={k}
                    className={`py-2 pr-2 text-right ${
                      c.weights[k] !== global[k]
                        ? "text-sky-400"
                        : "text-gray-200"
                    }`}
                  >
                    {c.weights[k] ?? "—"}
                  </td>
                ))}
              </tr>
            ))}
            {classes.length === 0 && (
              <Empty cols={keys.length + 2}>
                No per-class profiles yet — classes resolve to the global prior
                until they have enough trades.
              </Empty>
            )}
          </tbody>
        </table>
      </div>
    </LCard>
  );
}

// ── 5. Pair Learner ──────────────────────────────────────────────────────────
export function PairLearner({ d }) {
  const pairs = d?.pairs || [];
  function recVariant(rec) {
    if (rec === "AVOID") return "red";
    if (rec === "REDUCE_SIZE") return "yellow";
    if (rec === "TRADE") return "green";
    return "muted";
  }
  return (
    <LCard
      title="Pair Learner"
      on={d?.continuous_enabled}
      onLabel="CONTINUOUS"
      offLabel="BUCKETED"
    >
      <div className="mb-4 grid grid-cols-3 gap-3">
        <MiniStat label="Learned Pairs" value={d?.pair_count || 0} />
        <MiniStat
          label="Recommended"
          value={(d?.recommended || []).length}
          accent="text-emerald-400"
        />
        <MiniStat
          label="Avoid"
          value={d?.avoid_count || 0}
          accent="text-red-400"
        />
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[680px] text-sm">
          <thead>
            <tr className="border-b border-gray-700">
              <th className={TH}>Pair</th>
              <th className={THR}>Size ×</th>
              <th className={THR}>Win Rate</th>
              <th className={THR}>Trades</th>
              <th className={TH}>Recommendation</th>
              <th className={THR}>Entry Acc</th>
              <th className={THR}>Mgmt Score</th>
              <th className={THR}>Opt SL (R)</th>
            </tr>
          </thead>
          <tbody>
            {pairs.length === 0 && (
              <Empty cols={8}>
                No learned pairs yet — profiles build as trades close.
              </Empty>
            )}
            {pairs.map((p) => (
              <tr key={p.pair} className="border-b border-gray-800">
                <td className="py-2 pr-2 font-semibold text-gray-100">
                  {p.pair}
                </td>
                <td
                  className={`py-2 pr-2 text-right font-mono ${multColor(
                    p.multiplier,
                  )}`}
                >
                  {Number(p.multiplier).toFixed(2)}×
                </td>
                <td className={`py-2 pr-2 text-right ${accColor(p.win_rate)}`}>
                  {pct(p.win_rate)}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">{p.trades}</td>
                <td className="py-2 pr-2">
                  <span className={badgeClass(recVariant(p.recommendation))}>
                    {p.recommendation || "—"}
                  </span>
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {p.entry_accuracy === null ? "—" : pct(p.entry_accuracy)}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {p.management_score === null
                    ? "—"
                    : Number(p.management_score).toFixed(2)}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {p.optimal_sl_r === null
                    ? "—"
                    : Number(p.optimal_sl_r).toFixed(2)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </LCard>
  );
}

// ── 6. Counterfactual Attribution (L4) ───────────────────────────────────────
export function CounterfactualAttribution({ d }) {
  const modules = d?.modules || [];
  return (
    <LCard
      title="Counterfactual Attribution"
      on={d?.enabled}
      note="Leave-one-out decision replay — each module's MARGINAL contribution to the trades taken. Decisive = trade only happened because of it; removing the module would delete those trades (and their R)."
    >
      <div className="mb-4 grid grid-cols-3 gap-3">
        <MiniStat label="Modules Ranked" value={d?.module_count || 0} />
        <MiniStat label="Trades Analyzed" value={d?.trades_analyzed || 0} />
        <MiniStat label="Last Computed" value={tsAgo(d?.computed_at)} />
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-sm">
          <thead>
            <tr className="border-b border-gray-700">
              <th className={TH}>Module</th>
              <th className={THR}>Involved</th>
              <th className={THR}>Decisive</th>
              <th className={THR}>Marginal R</th>
              <th className={THR}>Exp. (decisive)</th>
              <th className={THR}>Sharpe</th>
              <th className={THR}>Drawdown</th>
              <th className={TH}>Better off without?</th>
            </tr>
          </thead>
          <tbody>
            {modules.length === 0 && (
              <Empty cols={8}>
                {d?.enabled
                  ? `No attribution computed yet — runs every ${d?.interval || 0} trades after ${d?.min_trades || 0} closed.`
                  : "Counterfactual engine disabled."}
              </Empty>
            )}
            {modules.map((m) => (
              <tr key={m.module} className="border-b border-gray-800">
                <td className="py-2 pr-2 font-semibold text-gray-100">
                  {m.module}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {m.trades_involved}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {m.decisive_trades}
                </td>
                <td
                  className={`py-2 pr-2 text-right ${multColor(
                    1 + (Number(m.marginal_r) || 0),
                  )}`}
                >
                  {(Number(m.marginal_r) || 0).toFixed(2)}R
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {(Number(m.expectancy_when_decisive) || 0).toFixed(2)}R
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {(Number(m.sharpe_contribution) || 0).toFixed(2)}
                </td>
                <td className="py-2 pr-2 text-right text-gray-500">
                  {(Number(m.drawdown_contribution) || 0).toFixed(2)}R
                </td>
                <td className="py-2 pr-2">
                  {m.better_off_without ? (
                    <span className={badgeClass("red")}>
                      yes ({(Number(m.r_difference) || 0).toFixed(2)}R)
                    </span>
                  ) : (
                    <span className={badgeClass("green")}>no</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </LCard>
  );
}

// ── 7. Module Interaction Discovery (L5b) ─────────────────────────────────────
function relColor(rel) {
  if (rel === "SYNERGY") return "text-emerald-400";
  if (rel === "TOXIC") return "text-red-400";
  return "text-gray-500";
}

export function ModuleInteractions({ d }) {
  const opt = d?.optimal_subset || {};
  const toxic = d?.toxic_pairs || [];
  const synergy = d?.synergy_pairs || [];
  const pairs = d?.pairs || [];
  const improvement = Number(opt.improvement) || 0;
  return (
    <LCard
      title="Module Interactions (L5b)"
      on={d?.enabled}
      note="Leave-K-out decision replay — non-additive interactions between modules. Toxic pairs reinforce each other's mistakes; synergistic pairs are worth more together than the sum of their parts. The optimal subset is the active-module configuration that would have produced the best book."
    >
      <div className="mb-4 grid grid-cols-3 gap-3">
        <MiniStat label="Modules Analyzed" value={d?.module_count || 0} />
        <MiniStat label="Trades Analyzed" value={d?.trades_analyzed || 0} />
        <MiniStat label="Last Computed" value={tsAgo(d?.computed_at)} />
      </div>

      <div className="mb-3">
        <p className="mb-1.5 text-xs font-semibold text-gray-300">
          Optimal Active Subset
          {opt.search ? (
            <span className="font-normal text-gray-500"> ({opt.search})</span>
          ) : null}
        </p>
        {(opt.active_modules || []).length === 0 ? (
          <p className="text-xs text-gray-500">
            {d?.enabled
              ? `No analysis computed yet — runs every ${d?.interval || 0} trades.`
              : "Interaction discovery disabled."}
          </p>
        ) : (
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex flex-wrap gap-1">
              {(opt.active_modules || []).map((m) => (
                <span key={m} className={badgeClass("green")}>
                  {m}
                </span>
              ))}
              {(opt.shadow_modules || []).map((m) => (
                <span key={m} className={badgeClass("red")}>
                  shadow: {m}
                </span>
              ))}
            </div>
            <div className="text-xs text-gray-400">
              Expected improvement:{" "}
              <b
                className={improvement > 0 ? "text-emerald-400" : "text-gray-500"}
              >
                {improvement > 0 ? "+" : ""}
                {improvement.toFixed(2)}R
              </b>{" "}
              <span className="text-gray-500">
                (book {(Number(opt.total_r) || 0).toFixed(2)}R vs baseline{" "}
                {(Number(opt.baseline_r) || 0).toFixed(2)}R, {opt.trades_taken || 0}{" "}
                trades)
              </span>
            </div>
          </div>
        )}
      </div>

      {(toxic.length > 0 || synergy.length > 0) && (
        <div className="mb-3 space-y-1 text-xs">
          {toxic.length > 0 && (
            <div>
              <span className={badgeClass("red")}>toxic</span>{" "}
              {toxic.map((p) => (
                <span
                  key={`${p.module_a}-${p.module_b}`}
                  className="mr-3 text-gray-300"
                >
                  {p.module_a}+{p.module_b} (
                  {(Number(p.interaction_effect) || 0).toFixed(2)}R)
                </span>
              ))}
            </div>
          )}
          {synergy.length > 0 && (
            <div>
              <span className={badgeClass("green")}>synergy</span>{" "}
              {synergy.map((p) => (
                <span
                  key={`${p.module_a}-${p.module_b}`}
                  className="mr-3 text-gray-300"
                >
                  {p.module_a}+{p.module_b} (+
                  {(Number(p.interaction_effect) || 0).toFixed(2)}R)
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full min-w-[680px] text-sm">
          <thead>
            <tr className="border-b border-gray-700">
              <th className={TH}>Module A</th>
              <th className={TH}>Module B</th>
              <th className={THR}>Δ remove A</th>
              <th className={THR}>Δ remove B</th>
              <th className={THR}>Δ remove A+B</th>
              <th className={THR}>Interaction</th>
              <th className={TH}>Relationship</th>
            </tr>
          </thead>
          <tbody>
            {pairs.length === 0 && (
              <Empty cols={7}>
                {d?.enabled
                  ? "No interaction matrix yet — needs at least 2 voting modules over recent trades."
                  : "Interaction discovery disabled."}
              </Empty>
            )}
            {pairs.map((p) => (
              <tr
                key={`${p.module_a}-${p.module_b}`}
                className="border-b border-gray-800"
              >
                <td className="py-2 pr-2 font-semibold text-gray-100">
                  {p.module_a}
                </td>
                <td className="py-2 pr-2 font-semibold text-gray-100">
                  {p.module_b}
                </td>
                <td className="py-2 pr-2 text-right text-gray-500">
                  {(Number(p.removal_delta_a) || 0).toFixed(2)}R
                </td>
                <td className="py-2 pr-2 text-right text-gray-500">
                  {(Number(p.removal_delta_b) || 0).toFixed(2)}R
                </td>
                <td className="py-2 pr-2 text-right text-gray-500">
                  {(Number(p.removal_delta_ab) || 0).toFixed(2)}R
                </td>
                <td
                  className={`py-2 pr-2 text-right font-semibold ${relColor(
                    p.relationship,
                  )}`}
                >
                  {(Number(p.interaction_effect) || 0).toFixed(2)}R
                </td>
                <td className={`py-2 pr-2 ${relColor(p.relationship)}`}>
                  {p.relationship}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </LCard>
  );
}

// ── 8. Parameter Evolution (L5a) ──────────────────────────────────────────────
export function ParameterEvolution({ d }) {
  const shadows = d?.active_shadows || [];
  const proms = d?.recent_promotions || [];
  return (
    <LCard
      title="Parameter Evolution (L5a)"
      on={d?.enabled}
      note="Explores consensus / ranker thresholds by replaying recent closed trades, walk-forward validates winners, then proves them over live closes (shadow) before recommending a promotion. It only proposes — applying a value is gated."
    >
      <div className="mb-4 grid grid-cols-3 gap-3">
        <MiniStat label="Active Shadows" value={d?.active_shadow_count || 0} />
        <MiniStat label="Replay Lookback" value={d?.replay_lookback || 0} />
        <MiniStat label="Shadow Trades" value={d?.shadow_validation_trades || 0} />
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[600px] text-sm">
          <thead>
            <tr className="border-b border-gray-700">
              <th className={TH}>Parameter</th>
              <th className={TH}>State</th>
              <th className={THR}>Current</th>
              <th className={THR}>Proposed</th>
              <th className={THR}>Shadow Trades</th>
              <th className={THR}>Improvement /trade</th>
            </tr>
          </thead>
          <tbody>
            {shadows.length === 0 && (
              <Empty cols={6}>
                {d?.enabled
                  ? "No candidates in shadow validation yet."
                  : "Parameter evolution disabled."}
              </Empty>
            )}
            {shadows.map((s, i) => (
              <tr key={`${s.param_name}-${i}`} className="border-b border-gray-800">
                <td className="py-2 pr-2 font-semibold text-gray-100">
                  {s.param_name}
                </td>
                <td className="py-2 pr-2">
                  <span className={badgeClass("blue")}>{s.state}</span>
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {Number(s.current_value).toFixed(3)}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {Number(s.proposed_value).toFixed(3)}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {s.shadow_trades}
                </td>
                <td
                  className={`py-2 pr-2 text-right ${multColor(
                    1 + (Number(s.improvement_per_trade) || 0),
                  )}`}
                >
                  {(Number(s.improvement_per_trade) || 0).toFixed(3)}R
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {proms.length > 0 && (
        <div className="mt-4 overflow-x-auto">
          <p className="mb-2 text-xs font-semibold text-gray-300">
            Recent decisions
          </p>
          <table className="w-full min-w-[480px] text-sm">
            <thead>
              <tr className="border-b border-gray-700">
                <th className={TH}>Parameter</th>
                <th className={TH}>Decision</th>
                <th className={THR}>Old</th>
                <th className={THR}>New</th>
                <th className={TH}>When</th>
              </tr>
            </thead>
            <tbody>
              {proms.map((p, i) => (
                <tr key={`${p.param_name}-${i}`} className="border-b border-gray-800">
                  <td className="py-2 pr-2 text-gray-200">{p.param_name}</td>
                  <td className="py-2 pr-2">
                    <span
                      className={badgeClass(
                        p.decision === "rejected" ? "red" : "green",
                      )}
                    >
                      {p.decision}
                    </span>
                  </td>
                  <td className="py-2 pr-2 text-right text-gray-200">
                    {Number(p.old_value).toFixed(3)}
                  </td>
                  <td className="py-2 pr-2 text-right text-gray-200">
                    {Number(p.new_value).toFixed(3)}
                  </td>
                  <td className="py-2 pr-2 text-gray-500">{tsAgo(p.decided_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </LCard>
  );
}

// ── 9. Signal Discovery (L5c) ─────────────────────────────────────────────────
export function SignalDiscovery({ d }) {
  const rules = d?.rules || [];
  return (
    <LCard
      title="Signal Discovery (L5c)"
      on={d?.enabled}
      note={`Mines module-condition combinations whose edge persists out-of-sample — rules nobody wrote, found in the data. Recommends candidates only; never auto-creates a live signal. Overfitting guards: Bonferroni α=${(Number(d?.bonferroni_alpha) || 0).toFixed(3)}, OOS retention ≥${Math.round((Number(d?.walk_forward_ratio_threshold) || 0) * 100)}%, active cap ${d?.max_active_signals || 0}.`}
    >
      <div className="mb-4 grid grid-cols-3 gap-3">
        <MiniStat label="Rules" value={d?.rule_count || 0} />
        <MiniStat
          label="Qualifying (OOS)"
          value={d?.qualifying_count || 0}
          accent="text-emerald-400"
        />
        <MiniStat
          label="Active / Cap"
          value={`${d?.active_count || 0} / ${d?.max_active_signals || 0}`}
        />
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-sm">
          <thead>
            <tr className="border-b border-gray-700">
              <th className={TH}>Rule</th>
              <th className={THR}>Support</th>
              <th className={THR}>Win%</th>
              <th className={THR}>Edge</th>
              <th className={THR}>Train edge</th>
              <th className={THR}>Test edge</th>
              <th className={THR}>OOS ret.</th>
              <th className={THR}>p</th>
              <th className={THR}>Score</th>
              <th className={TH}>Status</th>
            </tr>
          </thead>
          <tbody>
            {rules.length === 0 && (
              <Empty cols={10}>
                {d?.enabled
                  ? "No rules discovered yet — needs more closed trades."
                  : "Signal discovery disabled."}
              </Empty>
            )}
            {rules.map((r, i) => (
              <tr key={`${r.label}-${i}`} className="border-b border-gray-800">
                <td className="py-2 pr-2 font-semibold text-gray-100">{r.label}</td>
                <td className="py-2 pr-2 text-right text-gray-200">{r.support}</td>
                <td className={`py-2 pr-2 text-right ${accColor(r.win_rate)}`}>
                  {pct(r.win_rate)}
                </td>
                <td
                  className={`py-2 pr-2 text-right ${multColor(
                    1 + (Number(r.edge) || 0),
                  )}`}
                >
                  {(Number(r.edge) || 0).toFixed(2)}R
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {(Number(r.train_edge) || 0).toFixed(2)}R
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {(Number(r.test_edge) || 0).toFixed(2)}R
                </td>
                <td className="py-2 pr-2 text-right text-gray-500">
                  {Math.round((Number(r.wf_ratio) || 0) * 100)}%
                </td>
                <td className="py-2 pr-2 text-right text-gray-500">
                  {(Number(r.p_value) || 0).toFixed(3)}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {(Number(r.score) || 0).toFixed(2)}
                </td>
                <td className="py-2 pr-2">
                  {r.active ? (
                    <span className={badgeClass("green")}>active</span>
                  ) : r.qualifies ? (
                    <span className={badgeClass("blue")}>validated</span>
                  ) : (
                    <span className={badgeClass("muted")}>candidate</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </LCard>
  );
}

// ── 10. Virtual Voting Modules (L5c) ──────────────────────────────────────────
function vmModeBadge(mode) {
  if (mode === "ACTIVE") return <span className={badgeClass("green")}>active</span>;
  if (mode === "SHADOW") return <span className={badgeClass("blue")}>shadow</span>;
  return <span className={badgeClass("muted")}>disabled</span>;
}

export function VirtualModules({ d }) {
  const modules = d?.modules || [];
  const transitions = d?.transitions || [];
  const counts = d?.counts || {};
  const lastEval = d?.last_evaluation || null;
  return (
    <LCard
      title="Virtual Voting Modules (L5c)"
      on={d?.enabled}
      note={`Discovered rules promoted to synthetic voting modules that vote like a real module — but earn their way out of SHADOW first. Promotion gated: ≥${d?.shadow_trades_required || 0} shadow trades, accuracy ≥${pct(d?.min_shadow_accuracy)}, active cap ${d?.max_active || 0}. Kill switch: ${d?.kill_switch ? "on" : "OFF (all weights 0)"}; promotion: ${d?.promotion_enabled ? "on" : "off"}.${d?.restart_pending ? ` ${d.restart_pending} module(s) in restart-shadow.` : ""}`}
    >
      <div className="mb-4 grid grid-cols-3 gap-3">
        <MiniStat
          label="Active"
          value={counts.ACTIVE || 0}
          accent="text-emerald-400"
        />
        <MiniStat label="Shadow" value={counts.SHADOW || 0} />
        <MiniStat
          label="Disabled"
          value={counts.DISABLED || 0}
          accent="text-gray-400"
        />
      </div>
      {lastEval &&
      (lastEval.promoted?.length ||
        lastEval.retired?.length ||
        lastEval.registered?.length) ? (
        <p className="mb-3 text-xs text-gray-500">
          Last pass — registered: {(lastEval.registered || []).length}, promoted:{" "}
          {(lastEval.promoted || []).length}, retired:{" "}
          {(lastEval.retired || []).length}.
        </p>
      ) : null}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-sm">
          <thead>
            <tr className="border-b border-gray-700">
              <th className={TH}>Module</th>
              <th className={TH}>Dir</th>
              <th className={TH}>Mode</th>
              <th className={THR}>Weight</th>
              <th className={THR}>Live wt</th>
              <th className={THR}>Win%</th>
              <th className={THR}>Edge</th>
              <th className={TH}>Source rule</th>
            </tr>
          </thead>
          <tbody>
            {modules.length === 0 && (
              <Empty cols={8}>
                {d?.enabled
                  ? "No virtual modules yet — discovered rules register here as shadow candidates."
                  : "Virtual modules off (discovery engine not live)."}
              </Empty>
            )}
            {modules.map((m, i) => (
              <tr key={`${m.name}-${i}`} className="border-b border-gray-800">
                <td className="py-2 pr-2 text-xs font-semibold text-gray-100">
                  {m.name}
                </td>
                <td className="py-2 pr-2 text-gray-200">{m.vote_direction}</td>
                <td className="py-2 pr-2">
                  {vmModeBadge(m.mode)}
                  {m.restart_shadow ? (
                    <span className={`ml-1 ${badgeClass("muted")}`}>restart</span>
                  ) : null}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {(Number(m.weight) || 0).toFixed(2)}
                </td>
                <td
                  className={`py-2 pr-2 text-right ${
                    m.effective_weight > 0 ? "text-emerald-400" : "text-gray-500"
                  }`}
                >
                  {(Number(m.effective_weight) || 0).toFixed(2)}
                </td>
                <td className={`py-2 pr-2 text-right ${accColor(m.win_rate)}`}>
                  {pct(m.win_rate)}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {(Number(m.edge) || 0).toFixed(2)}R
                </td>
                <td className="py-2 pr-2 text-[10px] text-gray-500">
                  {m.source_label}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {transitions.length > 0 && (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[560px] text-sm">
            <thead>
              <tr className="border-b border-gray-700">
                <th className={TH}>Module</th>
                <th className={TH}>Transition</th>
                <th className={THR}>Weight</th>
                <th className={THR}>Acc</th>
                <th className={THR}>Marg R</th>
                <th className={TH}>Reason</th>
              </tr>
            </thead>
            <tbody>
              {transitions.map((t, i) => (
                <tr
                  key={`${t.name}-${t.timestamp}-${i}`}
                  className="border-b border-gray-800"
                >
                  <td className="py-2 pr-2 text-xs text-gray-200">{t.name}</td>
                  <td className="py-2 pr-2 text-gray-200">
                    {t.old_mode} → {t.new_mode}
                  </td>
                  <td className="py-2 pr-2 text-right text-gray-200">
                    {(Number(t.weight) || 0).toFixed(2)}
                  </td>
                  <td className="py-2 pr-2 text-right text-gray-200">
                    {pct(t.accuracy)}
                  </td>
                  <td className="py-2 pr-2 text-right text-gray-200">
                    {(Number(t.marginal_r) || 0).toFixed(3)}
                  </td>
                  <td className="py-2 pr-2 text-[10px] text-gray-500">
                    {t.reason}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </LCard>
  );
}

// ── 11. Capital Allocation (L5.5a) ────────────────────────────────────────────
export function CapitalAllocation({ d }) {
  const fps = d?.fingerprints || [];
  const rebals = d?.rebalance_history || [];
  const hw = d?.horizon_weights || {};
  return (
    <LCard
      title="Capital Allocation (L5.5a)"
      on={d?.enabled}
      note="Allocates capital across execution-style fingerprints (entry mode × horizon) by three-horizon expectancy — long horizon dominates so the book diversifies its alpha and never chases a short streak. A 1.0× across the board means too little history yet (no-op)."
    >
      <div className="mb-4 grid grid-cols-3 gap-3">
        <MiniStat label="Fingerprints" value={d?.fingerprint_count || 0} />
        <MiniStat
          label="Total Trades"
          value={d?.total_trades || 0}
          accent={d?.active ? "text-emerald-400" : "text-gray-400"}
        />
        <MiniStat
          label="Horizon Weights"
          value={`${Number(hw.short || 0).toFixed(1)}/${Number(hw.medium || 0).toFixed(1)}/${Number(hw.long || 0).toFixed(1)}`}
        />
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[680px] text-sm">
          <thead>
            <tr className="border-b border-gray-700">
              <th className={TH}>Strategy Fingerprint</th>
              <th className={THR}>Allocation</th>
              <th className={THR}>Size ×</th>
              <th className={THR}>Trades</th>
              <th className={THR}>Exp (short)</th>
              <th className={THR}>Exp (med)</th>
              <th className={THR}>Exp (long)</th>
            </tr>
          </thead>
          <tbody>
            {fps.length === 0 && (
              <Empty cols={7}>
                {d?.enabled
                  ? "No closed trades fingerprinted yet."
                  : "Capital allocation disabled."}
              </Empty>
            )}
            {fps.map((f, i) => (
              <tr key={`${f.fingerprint}-${i}`} className="border-b border-gray-800">
                <td className="py-2 pr-2 font-mono font-semibold text-gray-100">
                  {f.fingerprint}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {pct(f.allocation)}
                </td>
                <td
                  className={`py-2 pr-2 text-right font-mono ${multColor(
                    f.sizing_multiplier,
                  )}`}
                >
                  {Number(f.sizing_multiplier).toFixed(2)}×
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">{f.trades}</td>
                <td
                  className={`py-2 pr-2 text-right ${multColor(
                    1 + (Number(f.exp_short) || 0),
                  )}`}
                >
                  {(Number(f.exp_short) || 0).toFixed(2)}R
                </td>
                <td
                  className={`py-2 pr-2 text-right ${multColor(
                    1 + (Number(f.exp_medium) || 0),
                  )}`}
                >
                  {(Number(f.exp_medium) || 0).toFixed(2)}R
                </td>
                <td
                  className={`py-2 pr-2 text-right ${multColor(
                    1 + (Number(f.exp_long) || 0),
                  )}`}
                >
                  {(Number(f.exp_long) || 0).toFixed(2)}R
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rebals.length > 0 && (
        <div className="mt-4 overflow-x-auto">
          <p className="mb-2 text-xs font-semibold text-gray-300">
            Recent rebalances
          </p>
          <table className="w-full min-w-[420px] text-sm">
            <thead>
              <tr className="border-b border-gray-700">
                <th className={TH}>When</th>
                <th className={THR}>Fingerprints</th>
                <th className={THR}>Max shift</th>
                <th className={THR}>At floor</th>
              </tr>
            </thead>
            <tbody>
              {rebals.map((r, i) => (
                <tr key={`reb-${i}`} className="border-b border-gray-800">
                  <td className="py-2 pr-2 text-gray-500">{tsAgo(r.ts)}</td>
                  <td className="py-2 pr-2 text-right text-gray-200">
                    {r.fingerprints}
                  </td>
                  <td className="py-2 pr-2 text-right text-gray-200">
                    {Number(r.max_shift).toFixed(3)}
                  </td>
                  <td className="py-2 pr-2 text-right text-gray-200">
                    {r.floored}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </LCard>
  );
}

// ── 12. Execution Profiles (L5.5b) ────────────────────────────────────────────
export function ExecutionProfiles({ d }) {
  const profiles = d?.profiles || [];
  return (
    <LCard
      title="Execution Profiles (L5.5b)"
      on={d?.enabled}
      note="Per-trade execution-style parameter vectors (SL ATR ×, TP R:R, trailing, partial, min-score) selected from trade context (horizon × regime × consensus). Profiles are hypotheses, not identities — tuned, scored, and shadow/retired like any other component. Disabled or no-match → config defaults (no-op)."
    >
      <div className="mb-4 grid grid-cols-3 gap-3">
        <MiniStat
          label="Active Profiles"
          value={`${d?.active_count || 0}/${d?.max_active_profiles || 0}`}
        />
        <MiniStat label="Default" value={d?.default_profile || "—"} />
        <MiniStat label="Trades Scored" value={d?.total_trades || 0} />
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-sm">
          <thead>
            <tr className="border-b border-gray-700">
              <th className={TH}>Profile</th>
              <th className={THR}>SL ATR×</th>
              <th className={THR}>TP1/TP2 R:R</th>
              <th className={TH}>Trailing</th>
              <th className={THR}>Partial</th>
              <th className={THR}>Selections</th>
              <th className={THR}>Trades</th>
              <th className={THR}>Expectancy</th>
            </tr>
          </thead>
          <tbody>
            {profiles.length === 0 && (
              <Empty cols={8}>
                {d?.enabled
                  ? "No execution profiles yet."
                  : "Execution profiles disabled."}
              </Empty>
            )}
            {profiles.map((p, i) => (
              <tr
                key={`${p.name}-${i}`}
                className="border-b border-gray-800"
                style={{ opacity: p.active ? 1 : 0.45 }}
              >
                <td className="py-2 pr-2 font-mono font-semibold text-gray-100">
                  {p.name}
                  {p.builtin ? "" : " *"}
                  {p.active ? "" : " (retired)"}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {Number(p.sl_atr_multiplier).toFixed(2)}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {`${Number(p.tp_rr_ratio).toFixed(1)}/${Number(p.tp2_rr_ratio).toFixed(1)}`}
                </td>
                <td className="py-2 pr-2 text-gray-200">
                  {p.trailing_method}
                  {` @${Number(p.trailing_activation_r).toFixed(1)}R`}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {p.partial_exit_enabled ? pct(p.partial_exit_pct) : "—"}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {p.selections}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">{p.trades}</td>
                <td
                  className={`py-2 pr-2 text-right ${multColor(
                    1 + (Number(p.expectancy) || 0),
                  )}`}
                >
                  {p.scored ? `${(Number(p.expectancy) || 0).toFixed(2)}R` : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </LCard>
  );
}

// ── 13. Regime Detection (L7) ─────────────────────────────────────────────────
export function RegimeDetection({ d }) {
  const pairs = d?.pairs || [];
  const transitions = d?.transitions || [];
  const dist = d?.regime_distribution || {};
  return (
    <LCard title="Regime Detection (L7)" on={d?.enabled}>
      <div className="mb-4 grid grid-cols-3 gap-3">
        <MiniStat label="Pairs Classified" value={d?.pair_count || 0} />
        <MiniStat label="Hysteresis Bars" value={d?.hysteresis_bars || 0} />
        <MiniStat label="Lookback Bars" value={d?.lookback_bars || 0} />
      </div>
      <div className="mb-3 flex flex-wrap gap-1.5">
        {Object.keys(dist).length === 0 ? (
          <span className="text-xs text-gray-500">No regimes classified yet.</span>
        ) : (
          Object.entries(dist).map(([reg, n]) => (
            <span key={reg} className={badgeClass("muted")}>
              {reg}: {n}
            </span>
          ))
        )}
      </div>
      {pairs.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[520px] text-sm">
            <thead>
              <tr className="border-b border-gray-700">
                <th className={TH}>Pair</th>
                <th className={TH}>Regime</th>
                <th className={THR}>Confidence</th>
                <th className={THR}>Dir.Str</th>
                <th className={THR}>Vol</th>
                <th className={THR}>MeanRev</th>
              </tr>
            </thead>
            <tbody>
              {pairs.slice(0, 30).map((p) => (
                <tr key={p.pair} className="border-b border-gray-800">
                  <td className="py-2 pr-2 text-gray-200">{p.pair}</td>
                  <td className="py-2 pr-2 text-gray-200">{p.regime}</td>
                  <td className="py-2 pr-2 text-right text-gray-200">
                    {pct(p.confidence)}
                  </td>
                  <td className="py-2 pr-2 text-right text-gray-200">
                    {p.directional_strength}
                  </td>
                  <td className="py-2 pr-2 text-right text-gray-200">
                    {p.volatility_ratio}
                  </td>
                  <td className="py-2 pr-2 text-right text-gray-200">
                    {p.mean_reversion}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {transitions.length > 0 && (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[420px] text-sm">
            <thead>
              <tr className="border-b border-gray-700">
                <th className={TH}>Pair</th>
                <th className={TH}>Transition</th>
                <th className={THR}>Conf</th>
                <th className={THR}>When</th>
              </tr>
            </thead>
            <tbody>
              {transitions.slice(0, 15).map((t, i) => (
                <tr key={i} className="border-b border-gray-800">
                  <td className="py-2 pr-2 text-gray-200">{t.pair}</td>
                  <td className="py-2 pr-2 text-gray-200">
                    {t.old_regime} → {t.new_regime}
                  </td>
                  <td className="py-2 pr-2 text-right text-gray-200">
                    {pct(t.confidence)}
                  </td>
                  <td className="py-2 pr-2 text-right text-gray-500">
                    {tsAgo(t.ts)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </LCard>
  );
}

// ── 14. Risk Management (live RiskEngine) ─────────────────────────────────────
export function RiskManagement({ d }) {
  const events = d?.risk_events || [];
  const correlations = d?.correlations || [];
  const state = d?.risk_mode || d?.state || "NORMAL";
  const stateOk = state === "NORMAL";
  const dailyPnl = Number(d?.daily_pnl_dollars) || 0;
  const lossUsed = Number(d?.daily_loss_used_pct) || 0;
  return (
    <LCard
      title="Risk Management (RiskEngine)"
      on={d?.enabled}
      note="Live account-survival authority — balance, daily P&L, drawdown, risk mode and how much of the daily-loss budget is used. Sourced directly from the RiskEngine (DrawdownGuard + P&L tracker)."
    >
      <div className="mb-4 grid grid-cols-3 gap-3">
        <MiniStat
          label="Risk Mode"
          value={state}
          accent={stateOk ? "text-emerald-400" : "text-red-400"}
        />
        <MiniStat label="Balance" value={d?.balance != null ? `$${d.balance}` : "—"} />
        <MiniStat
          label="Daily P&L"
          value={`$${dailyPnl.toFixed(2)} (${d?.daily_pnl_pct != null ? d.daily_pnl_pct : 0}%)`}
          accent={dailyPnl >= 0 ? "text-emerald-400" : "text-red-400"}
        />
      </div>
      <div className="mb-4 grid grid-cols-3 gap-3">
        <MiniStat
          label="Daily Loss Used"
          value={`${lossUsed}%`}
          accent={lossUsed >= 80 ? "text-red-400" : "text-gray-100"}
        />
        <MiniStat
          label="Rolling Drawdown"
          value={`${d?.rolling_drawdown_pct || 0}%`}
        />
        <MiniStat
          label="Sizing Factor"
          value={d?.sizing_factor != null ? d.sizing_factor : 1}
          accent={multColor(d?.sizing_factor)}
        />
      </div>
      <div className="mb-4 grid grid-cols-3 gap-3">
        <MiniStat
          label="Daily Loss Limit"
          value={`${d?.daily_loss_limit_pct || 0}%`}
        />
        <MiniStat label="Peak Equity" value={d?.peak_equity || 0} />
        <MiniStat
          label="Flatten?"
          value={d?.should_flatten ? "YES" : "no"}
          accent={d?.should_flatten ? "text-red-400" : "text-gray-100"}
        />
      </div>
      {events.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[480px] text-sm">
            <thead>
              <tr className="border-b border-gray-700">
                <th className={TH}>Pair</th>
                <th className={TH}>Rule</th>
                <th className={TH}>Reason</th>
                <th className={THR}>When</th>
              </tr>
            </thead>
            <tbody>
              {events.slice(0, 20).map((e, i) => (
                <tr key={i} className="border-b border-gray-800">
                  <td className="py-2 pr-2 text-gray-200">{e.pair}</td>
                  <td className="py-2 pr-2 text-gray-200">{e.rule}</td>
                  <td className="py-2 pr-2 text-gray-300">{e.reason}</td>
                  <td className="py-2 pr-2 text-right text-gray-500">
                    {tsAgo(e.ts)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {correlations.length > 0 && (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[360px] text-sm">
            <thead>
              <tr className="border-b border-gray-700">
                <th className={TH}>Pair A</th>
                <th className={TH}>Pair B</th>
                <th className={THR}>Correlation</th>
              </tr>
            </thead>
            <tbody>
              {correlations.slice(0, 15).map((c, i) => (
                <tr key={i} className="border-b border-gray-800">
                  <td className="py-2 pr-2 text-gray-200">{c.pair_a}</td>
                  <td className="py-2 pr-2 text-gray-200">{c.pair_b}</td>
                  <td className="py-2 pr-2 text-right text-gray-200">
                    {c.correlation}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </LCard>
  );
}

// ── 15. Behaviour Discovery (L6) ──────────────────────────────────────────────
export function BehaviorDiscovery({ d }) {
  const behaviors = d?.behaviors || [];
  const counts = d?.counts || {};
  const stateColor = (s) =>
    s === "ACTIVE"
      ? "text-emerald-400"
      : s === "RETIRED"
        ? "text-gray-500"
        : "text-yellow-400";
  return (
    <LCard
      title="Behaviour Discovery (L6)"
      on={d?.enabled}
      note="Clusters each closed trade's execution feature vector into emergent behaviours nobody hard-coded, then walks each through a SHADOW → ACTIVE → RETIRED lifecycle by Bayesian-shrunk expectancy. Purely advisory; dormant until enough trades are recorded."
    >
      <div className="mb-4 grid grid-cols-3 gap-3">
        <MiniStat
          label="Clusters / Behaviours"
          value={`${d?.cluster_count || 0}/${d?.behavior_count || 0}`}
        />
        <MiniStat
          label="Active / Shadow / Retired"
          value={`${counts.ACTIVE || 0} / ${counts.SHADOW || 0} / ${counts.RETIRED || 0}`}
        />
        <MiniStat label="Trades Recorded" value={d?.total_trades || 0} />
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[680px] text-sm">
          <thead>
            <tr className="border-b border-gray-700">
              <th className={TH}>Behaviour</th>
              <th className={TH}>State</th>
              <th className={TH}>Profile</th>
              <th className={TH}>Horizon</th>
              <th className={THR}>Trades</th>
              <th className={THR}>Win%</th>
              <th className={THR}>Expectancy</th>
              <th className={THR}>Pctile</th>
            </tr>
          </thead>
          <tbody>
            {behaviors.length === 0 && (
              <Empty cols={8}>
                {d?.enabled
                  ? "No behaviours discovered yet — accruing trades."
                  : "Behaviour discovery disabled."}
              </Empty>
            )}
            {behaviors.map((b, i) => {
              const cat = (b.centroid && b.centroid.categorical) || {};
              return (
                <tr
                  key={`${b.behavior_id}-${i}`}
                  className="border-b border-gray-800"
                  style={{ opacity: b.state === "RETIRED" ? 0.45 : 1 }}
                >
                  <td className="py-2 pr-2 font-mono text-gray-100">
                    {b.behavior_id}
                  </td>
                  <td
                    className={`py-2 pr-2 font-semibold ${stateColor(b.state)}`}
                  >
                    {b.state}
                  </td>
                  <td className="py-2 pr-2 text-gray-200">{cat.PROFILE || "—"}</td>
                  <td className="py-2 pr-2 text-gray-200">{cat.HORIZON || "—"}</td>
                  <td className="py-2 pr-2 text-right text-gray-200">{b.trades}</td>
                  <td className="py-2 pr-2 text-right text-gray-200">
                    {pct(b.win_rate)}
                  </td>
                  <td
                    className={`py-2 pr-2 text-right ${multColor(
                      1 + (Number(b.shrunk_expectancy) || 0),
                    )}`}
                  >
                    {`${(Number(b.shrunk_expectancy) || 0).toFixed(2)}R`}
                  </td>
                  <td className="py-2 pr-2 text-right text-gray-200">
                    {(Number(b.percentile) || 0).toFixed(2)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </LCard>
  );
}

// ── 16. Tuner Agent ───────────────────────────────────────────────────────────
export function TunerAgent({ d }) {
  const tunables = d?.tunables || [];
  const audit = d?.audit || [];
  const missing = d?.unregistered_expected || [];
  const bypass = d?.bypass_attempts || [];
  return (
    <LCard
      title="Tuner Agent"
      on={d?.enabled}
      onLabel="SOLE AUTHORITY"
      offLabel="OFF"
    >
      <div className="mb-4 grid grid-cols-3 gap-3">
        <MiniStat
          label="Registered"
          value={`${d?.registered_count || 0}/${d?.expected_count || 0}`}
        />
        <MiniStat
          label="Disabled (3-strike)"
          value={(d?.disabled_tunables || []).length}
          accent={
            (d?.disabled_tunables || []).length
              ? "text-red-400"
              : "text-gray-100"
          }
        />
        <MiniStat
          label="Bypass Attempts"
          value={bypass.length}
          accent={bypass.length ? "text-red-400" : "text-gray-100"}
        />
      </div>
      {missing.length > 0 && (
        <p className="mb-2 text-xs text-yellow-400">
          Unregistered expected: {missing.join(", ")}
        </p>
      )}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[600px] text-sm">
          <thead>
            <tr className="border-b border-gray-700">
              <th className={TH}>Tunable</th>
              <th className={TH}>Frequency</th>
              <th className={THR}>Tunes</th>
              <th className={THR}>Last Tune</th>
              <th className={THR}>Failures</th>
              <th className={TH}>State</th>
            </tr>
          </thead>
          <tbody>
            {tunables.length === 0 && (
              <Empty cols={6}>
                {d?.enabled
                  ? "No tunables registered."
                  : "Tuner agent disabled — components self-tune."}
              </Empty>
            )}
            {tunables.map((t) => (
              <tr key={t.name} className="border-b border-gray-800">
                <td className="py-2 pr-2 font-semibold text-gray-100">{t.name}</td>
                <td className="py-2 pr-2 text-gray-500">{t.frequency}</td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {t.tune_count}
                </td>
                <td className="py-2 pr-2 text-right text-gray-200">
                  {tsAgo(t.last_tune_time)}
                </td>
                <td
                  className={`py-2 pr-2 text-right ${
                    t.consecutive_failures ? "text-red-400" : "text-gray-100"
                  }`}
                >
                  {t.consecutive_failures}
                </td>
                <td className="py-2 pr-2">
                  {t.disabled ? (
                    <span className={badgeClass("red")}>disabled</span>
                  ) : (
                    <span className={badgeClass("green")}>active</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {audit.length > 0 && (
        <div className="mt-4 overflow-x-auto">
          <p className="mb-2 text-xs font-semibold text-gray-300">
            Recent tune results
          </p>
          <table className="w-full min-w-[480px] text-sm">
            <thead>
              <tr className="border-b border-gray-700">
                <th className={TH}>When</th>
                <th className={TH}>Tunable</th>
                <th className={TH}>Result</th>
                <th className={TH}>Detail</th>
              </tr>
            </thead>
            <tbody>
              {audit.map((a, i) => (
                <tr key={i} className="border-b border-gray-800">
                  <td className="py-2 pr-2 text-gray-200">{tsAgo(a.timestamp)}</td>
                  <td className="py-2 pr-2 text-gray-200">{a.tunable_name}</td>
                  <td className="py-2 pr-2">
                    {a.rollback_performed ? (
                      <span className={badgeClass("orange")}>rolled back</span>
                    ) : a.skipped ? (
                      <span className={badgeClass("muted")}>skipped</span>
                    ) : a.changed ? (
                      <span className={badgeClass("green")}>applied</span>
                    ) : a.success ? (
                      <span className={badgeClass("blue")}>no change</span>
                    ) : (
                      <span className={badgeClass("red")}>failed</span>
                    )}
                  </td>
                  <td className="py-2 pr-2 text-[11px] text-gray-500">
                    {a.error || a.reason || "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </LCard>
  );
}
