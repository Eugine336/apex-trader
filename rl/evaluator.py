"""
APEX RL — Walk-Forward Evaluation Framework
=============================================
Out-of-sample, regime-stratified evaluation for trained RL agents.
Prevents overfitting by requiring consistent performance across
chronological folds and volatility regimes.
"""

from __future__ import annotations

import numpy as np
from dataclasses import dataclass, field
from typing import Optional

from .contracts import OBS_FEATURES, N_CONTEXT_FEATURES, build_symbol_vocab


@dataclass
class FoldResult:
    fold: int
    n_trades: int
    win_rate: float
    expectancy: float
    max_drawdown: float
    sharpe_ratio: float
    regime_metrics: dict = field(default_factory=dict)


@dataclass
class EvalReport:
    instrument: str
    n_folds: int
    total_trades: int
    mean_win_rate: float
    std_win_rate: float
    mean_expectancy: float
    std_expectancy: float
    mean_max_drawdown: float
    std_max_drawdown: float
    mean_sharpe: float
    std_sharpe: float
    folds: list = field(default_factory=list)
    regime_summary: dict = field(default_factory=dict)
    consistent: bool = False


def _classify_regime(atr_values: np.ndarray, idx: int) -> str:
    if len(atr_values) < 50 or idx < 50:
        return "normal"
    window = atr_values[max(0, idx - 100) : idx]
    if len(window) < 20:
        return "normal"
    percentile = np.searchsorted(np.sort(window), atr_values[idx]) / len(window)
    if percentile > 0.75:
        return "high_volatility"
    if percentile < 0.25:
        return "low_volatility"
    return "normal"


def _compute_sharpe(r_values: list[float]) -> float:
    if len(r_values) < 2:
        return 0.0
    arr = np.array(r_values)
    std = arr.std()
    if std < 1e-8:
        return 0.0
    return float(arr.mean() / std)


def _compute_max_drawdown(r_values: list[float]) -> float:
    if not r_values:
        return 0.0
    equity = np.cumsum(r_values)
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / (np.abs(peak) + 1e-8)
    return float(dd.max())


class WalkForwardEvaluator:
    """Chronological walk-forward evaluation with regime stratification."""

    def __init__(self, n_folds: int = 5):
        self.n_folds = n_folds

    def evaluate(
        self,
        agent,
        data_dir: str,
        instrument: str,
        initial_balance: float = 10_000.0,
    ) -> EvalReport:
        from .mtf_environment import ApexMultiTFTradingEnv

        env = ApexMultiTFTradingEnv(
            data_dir=data_dir,
            instrument=instrument,
            initial_balance=initial_balance,
        )

        total_bars = len(env._m5_feat)
        fold_size = total_bars // self.n_folds
        if fold_size < 100:
            raise ValueError(f"Insufficient data: {total_bars} bars / {self.n_folds} folds = {fold_size}")

        atr_col = env._m5_feat["atr"].values if "atr" in env._m5_feat.columns else np.ones(total_bars)

        folds: list[FoldResult] = []

        for fold_idx in range(self.n_folds):
            start = fold_idx * fold_size
            end = min((fold_idx + 1) * fold_size, total_bars - 1)

            env.idx = max(start, 70)
            env.balance = initial_balance
            env.trade = None
            env.equity_curve = [initial_balance]
            env.trades_log = []
            env.peak_equity = initial_balance

            regime_trades: dict[str, list[float]] = {
                "high_volatility": [],
                "low_volatility": [],
                "normal": [],
            }

            while env.idx < end:
                obs, ctx, sym_id = env._observe()
                action, conf, exp_r = agent.predict(obs, context_vec=ctx, symbol_id=sym_id)

                prev_n_trades = len(env.trades_log)
                _, reward, done, info = env.step(action)

                if len(env.trades_log) > prev_n_trades:
                    last_trade = env.trades_log[-1]
                    regime = _classify_regime(atr_col, env.idx)
                    regime_trades[regime].append(last_trade["r"])

                if done:
                    break

            r_values = [t["r"] for t in env.trades_log]
            n_trades = len(r_values)
            wins = [r for r in r_values if r > 0]
            wr = len(wins) / n_trades if n_trades > 0 else 0.0
            exp = float(np.mean(r_values)) if r_values else 0.0
            dd = _compute_max_drawdown(r_values)
            sharpe = _compute_sharpe(r_values)

            regime_metrics = {}
            for regime, rv in regime_trades.items():
                if rv:
                    regime_metrics[regime] = {
                        "n_trades": len(rv),
                        "win_rate": round(len([r for r in rv if r > 0]) / len(rv), 4),
                        "expectancy": round(float(np.mean(rv)), 4),
                    }

            folds.append(
                FoldResult(
                    fold=fold_idx,
                    n_trades=n_trades,
                    win_rate=round(wr, 4),
                    expectancy=round(exp, 4),
                    max_drawdown=round(dd, 4),
                    sharpe_ratio=round(sharpe, 4),
                    regime_metrics=regime_metrics,
                )
            )

        wrs = [f.win_rate for f in folds]
        exps = [f.expectancy for f in folds]
        dds = [f.max_drawdown for f in folds]
        sharpes = [f.sharpe_ratio for f in folds]

        regime_summary: dict[str, dict] = {}
        for regime in ["high_volatility", "low_volatility", "normal"]:
            all_r = []
            for f in folds:
                rm = f.regime_metrics.get(regime, {})
                if rm:
                    all_r.extend([rm["expectancy"]] * rm.get("n_trades", 1))
            if all_r:
                regime_summary[regime] = {
                    "mean_expectancy": round(float(np.mean(all_r)), 4),
                    "n_samples": len(all_r),
                }

        profitable_folds = sum(1 for e in exps if e > 0)
        consistent = profitable_folds >= (self.n_folds * 0.6)

        return EvalReport(
            instrument=instrument,
            n_folds=self.n_folds,
            total_trades=sum(f.n_trades for f in folds),
            mean_win_rate=round(float(np.mean(wrs)), 4),
            std_win_rate=round(float(np.std(wrs)), 4),
            mean_expectancy=round(float(np.mean(exps)), 4),
            std_expectancy=round(float(np.std(exps)), 4),
            mean_max_drawdown=round(float(np.mean(dds)), 4),
            std_max_drawdown=round(float(np.std(dds)), 4),
            mean_sharpe=round(float(np.mean(sharpes)), 4),
            std_sharpe=round(float(np.std(sharpes)), 4),
            folds=[f for f in folds],
            regime_summary=regime_summary,
            consistent=consistent,
        )


def evaluate_checkpoint(
    checkpoint_path: str,
    data_dir: str,
    instrument: str,
    n_folds: int = 5,
) -> EvalReport:
    """Load a checkpoint and run walk-forward evaluation."""
    import torch
    from .network import ApexRLAgent
    from .contracts import assert_compatible

    ckpt = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    meta = ckpt.get("meta", {})
    assert_compatible(meta)

    agent = ApexRLAgent(
        n_features=meta.get("n_features", OBS_FEATURES),
        context_dim=meta.get("context_dim", N_CONTEXT_FEATURES),
        n_symbols=meta.get("n_symbols", 0),
    )
    agent.load_state_dict(ckpt["agent"])
    agent.eval()

    evaluator = WalkForwardEvaluator(n_folds=n_folds)
    return evaluator.evaluate(agent, data_dir, instrument)
