"""
APEX RL — Multi-Timeframe PPO Trainer
=======================================
Trains the full 48-feature, symbol-conditioned generalist agent
using ``ApexMultiTFTradingEnv``.

Produces checkpoints that pass ``rl.contracts.assert_compatible()``
and are directly loadable by ``ShadowEngine`` / ``RLBridge``.

The legacy ``PPOTrainer`` (single-TF, 12 features) is untouched.
"""

from __future__ import annotations

import json
import time
import numpy as np
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import torch
import torch.nn as nn
import torch.optim as optim
from torch.distributions import Categorical

from .mtf_environment import ApexMultiTFTradingEnv
from .network import ApexRLAgent, ACTION_LABELS
from .vec_env import make_vec_env, has_instrument_data
from .contracts import (
    OBS_CONTRACT_VERSION,
    OBS_FEATURES,
    OBS_SHAPE,
    N_CONTEXT_FEATURES,
    build_symbol_vocab,
    schema_hash,
    schema,
)


@dataclass
class MTFPPOConfig:
    data_dir: str = "data"
    instrument: str = "EURUSD"
    commission_per_lot: float = 3.5
    slippage_factor: float = 0.3

    rollout_steps: int = 2048
    n_epochs: int = 10
    batch_size: int = 2048

    # Parallel rollout collection. ``n_envs`` copies of the environment are
    # stepped simultaneously (in separate processes when > 1) to keep the GPU
    # fed. ``n_envs=1`` runs the in-process fallback and is equivalent to the
    # original single-environment trainer. ``vec_start_method`` overrides the
    # multiprocessing start method ("fork"/"spawn"/"forkserver"); None auto-picks.
    n_envs: int = 8
    vec_start_method: Optional[str] = None

    clip_eps: float = 0.2
    vf_coef: float = 0.5
    ent_coef: float = 0.01
    max_grad_norm: float = 0.5

    lr: float = 3e-4
    lr_anneal: bool = True

    gamma: float = 0.99
    gae_lambda: float = 0.95

    total_steps: int = 2_000_000
    eval_interval: int = 50_000
    save_interval: int = 100_000
    log_interval: int = 10_000

    save_dir: str = "checkpoints"
    log_path: str = "training_log_mtf.json"

    reward_shaping: Optional[dict] = None


class MTFRolloutBuffer:
    """Stores experience with context vectors and symbol IDs for MTF training."""

    def __init__(self, steps: int, obs_shape: tuple, context_dim: int, device: torch.device):
        self.steps = steps
        self.device = device
        self.obs = torch.zeros(steps, *obs_shape)
        self.contexts = torch.zeros(steps, context_dim)
        self.symbol_ids = torch.zeros(steps, dtype=torch.long)
        self.actions = torch.zeros(steps, dtype=torch.long)
        self.log_probs = torch.zeros(steps)
        self.rewards = torch.zeros(steps)
        self.values = torch.zeros(steps)
        self.dones = torch.zeros(steps)
        self.returns: Optional[torch.Tensor] = None
        self.advantages: Optional[torch.Tensor] = None
        self.ptr = 0

    def add(self, obs, context, symbol_id, action, log_prob, reward, value, done):
        self.obs[self.ptr] = torch.FloatTensor(obs)
        self.contexts[self.ptr] = torch.FloatTensor(context)
        self.symbol_ids[self.ptr] = symbol_id
        self.actions[self.ptr] = action
        self.log_probs[self.ptr] = log_prob
        self.rewards[self.ptr] = reward
        self.values[self.ptr] = value
        self.dones[self.ptr] = done
        self.ptr += 1

    def compute_returns(self, last_value: float, gamma: float, gae_lambda: float):
        advantages = torch.zeros(self.steps)
        last_gae = 0.0
        for t in reversed(range(self.steps)):
            if t == self.steps - 1:
                next_val = last_value
                next_done = 0.0
            else:
                next_val = float(self.values[t + 1])
                next_done = float(self.dones[t + 1])
            delta = (
                self.rewards[t]
                + gamma * next_val * (1 - next_done)
                - self.values[t]
            )
            last_gae = delta + gamma * gae_lambda * (1 - next_done) * last_gae
            advantages[t] = last_gae
        self.returns = advantages + self.values
        self.advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

    def get_batches(self, batch_size: int):
        idx = torch.randperm(self.steps)
        for start in range(0, self.steps, batch_size):
            b = idx[start : start + batch_size]
            yield (
                self.obs[b].to(self.device),
                self.contexts[b].to(self.device),
                self.symbol_ids[b].to(self.device),
                self.actions[b].to(self.device),
                self.log_probs[b].to(self.device),
                self.returns[b].to(self.device),
                self.advantages[b].to(self.device),
            )

    def reset(self):
        self.ptr = 0


class MTFVecRolloutBuffer:
    """Rollout storage for ``n_envs`` environments stepped in parallel.

    Stores tensors of shape ``(steps, n_envs, ...)``. GAE is computed per
    environment column (episode boundaries are independent across envs) using
    the same convention as :class:`MTFRolloutBuffer`, and advantages are
    normalised globally over the full ``steps * n_envs`` batch — so with
    ``n_envs == 1`` the maths is identical to the single-env buffer.
    """

    def __init__(self, steps: int, n_envs: int, obs_shape: tuple, context_dim: int, device: torch.device):
        self.steps = steps
        self.n_envs = n_envs
        self.device = device
        # Pin host buffers so CPU→GPU copies in ``get_batches`` can run
        # asynchronously (``non_blocking=True``). Pinning requires CUDA; on a
        # CPU-only device it is a no-op and ``non_blocking`` is simply ignored.
        self._pin = device.type == "cuda"
        self.obs = torch.zeros(steps, n_envs, *obs_shape)
        self.contexts = torch.zeros(steps, n_envs, context_dim)
        self.symbol_ids = torch.zeros(steps, n_envs, dtype=torch.long)
        self.actions = torch.zeros(steps, n_envs, dtype=torch.long)
        self.log_probs = torch.zeros(steps, n_envs)
        self.rewards = torch.zeros(steps, n_envs)
        self.values = torch.zeros(steps, n_envs)
        self.dones = torch.zeros(steps, n_envs)
        if self._pin:
            self.obs = self.obs.pin_memory()
            self.contexts = self.contexts.pin_memory()
            self.symbol_ids = self.symbol_ids.pin_memory()
            self.actions = self.actions.pin_memory()
            self.log_probs = self.log_probs.pin_memory()
            self.rewards = self.rewards.pin_memory()
            self.values = self.values.pin_memory()
            self.dones = self.dones.pin_memory()
        self.returns: Optional[torch.Tensor] = None
        self.advantages: Optional[torch.Tensor] = None
        self.ptr = 0

    def add(self, obs, contexts, symbol_ids, actions, log_probs, rewards, values, dones):
        """Append one parallel transition; every argument is shape ``(n_envs, ...)``."""
        i = self.ptr
        self.obs[i] = torch.as_tensor(np.asarray(obs), dtype=torch.float32)
        self.contexts[i] = torch.as_tensor(np.asarray(contexts), dtype=torch.float32)
        self.symbol_ids[i] = torch.as_tensor(np.asarray(symbol_ids), dtype=torch.long)
        self.actions[i] = torch.as_tensor(np.asarray(actions), dtype=torch.long)
        self.log_probs[i] = torch.as_tensor(np.asarray(log_probs), dtype=torch.float32)
        self.rewards[i] = torch.as_tensor(np.asarray(rewards), dtype=torch.float32)
        self.values[i] = torch.as_tensor(np.asarray(values), dtype=torch.float32)
        self.dones[i] = torch.as_tensor(np.asarray(dones), dtype=torch.float32)
        self.ptr += 1

    def compute_returns(self, last_values, gamma: float, gae_lambda: float):
        last_values = torch.as_tensor(np.asarray(last_values), dtype=torch.float32).reshape(self.n_envs)
        advantages = torch.zeros(self.steps, self.n_envs)
        last_gae = torch.zeros(self.n_envs)
        for t in reversed(range(self.steps)):
            if t == self.steps - 1:
                next_val = last_values
                next_done = torch.zeros(self.n_envs)
            else:
                next_val = self.values[t + 1]
                next_done = self.dones[t + 1]
            delta = (
                self.rewards[t]
                + gamma * next_val * (1 - next_done)
                - self.values[t]
            )
            last_gae = delta + gamma * gae_lambda * (1 - next_done) * last_gae
            advantages[t] = last_gae
        self.returns = advantages + self.values
        self.advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)
        if self._pin:
            self.returns = self.returns.pin_memory()
            self.advantages = self.advantages.pin_memory()

    def get_batches(self, batch_size: int):
        total = self.steps * self.n_envs
        obs = self.obs.reshape(total, *self.obs.shape[2:])
        contexts = self.contexts.reshape(total, self.contexts.shape[2])
        symbol_ids = self.symbol_ids.reshape(total)
        actions = self.actions.reshape(total)
        log_probs = self.log_probs.reshape(total)
        returns = self.returns.reshape(total)
        advantages = self.advantages.reshape(total)
        idx = torch.randperm(total)
        for start in range(0, total, batch_size):
            b = idx[start : start + batch_size]
            yield (
                obs[b].to(self.device, non_blocking=True),
                contexts[b].to(self.device, non_blocking=True),
                symbol_ids[b].to(self.device, non_blocking=True),
                actions[b].to(self.device, non_blocking=True),
                log_probs[b].to(self.device, non_blocking=True),
                returns[b].to(self.device, non_blocking=True),
                advantages[b].to(self.device, non_blocking=True),
            )

    def reset(self):
        self.ptr = 0


class MTFPPOTrainer:
    """
    Trains the full MTF agent via PPO.

    Produces checkpoints compatible with production inference.
    """

    def __init__(self, cfg: MTFPPOConfig):
        self.cfg = cfg
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        print(f"[MTFTrainer] Device: {self.device}")

        self._vocab = build_symbol_vocab()

        self.env = ApexMultiTFTradingEnv(
            data_dir=cfg.data_dir,
            instrument=cfg.instrument,
            commission_per_lot=cfg.commission_per_lot,
            slippage_factor=cfg.slippage_factor,
        )
        if cfg.reward_shaping:
            self.env._reward_shaping = cfg.reward_shaping

        self.agent = ApexRLAgent(
            n_features=OBS_FEATURES,
            n_actions=self.env.action_space_n(),
            context_dim=N_CONTEXT_FEATURES,
            n_symbols=len(self._vocab),
        ).to(self.device)

        print(f"[MTFTrainer] Agent parameters: {self.agent.count_parameters():,}")

        self.opt = optim.Adam(self.agent.parameters(), lr=cfg.lr, eps=1e-5)

        Path(cfg.save_dir).mkdir(exist_ok=True)
        self.log: list[dict] = []
        self.global_step = 0
        self.best_reward = -np.inf

    def _env_kwargs(self, instrument: str) -> dict:
        """Picklable kwargs to construct one environment for *instrument*."""
        return {
            "data_dir": self.cfg.data_dir,
            "instrument": instrument,
            "commission_per_lot": self.cfg.commission_per_lot,
            "slippage_factor": self.cfg.slippage_factor,
            "reward_shaping": self.cfg.reward_shaping,
        }

    def train(self):
        """Train on the single configured instrument with ``n_envs`` parallel copies."""
        kwargs_list = [self._env_kwargs(self.cfg.instrument) for _ in range(max(1, self.cfg.n_envs))]
        vec = make_vec_env(kwargs_list, start_method=self.cfg.vec_start_method)
        try:
            self._run_loop(vec, self.cfg.total_steps, label=f" ({self.cfg.instrument})")
            self.save("final")
            self._flush_log()
        finally:
            vec.close()
        print(f"[MTFTrainer] Training complete. Best reward: {self.best_reward:.4f}")

    def train_curriculum(self, instruments: list[str]):
        """Round-robin curriculum: one stage per instrument, shared agent.

        A single worker pool of ``n_envs`` processes is created once and its
        environments are rebuilt (``reload``) for each instrument, avoiding the
        cost of respawning processes 49 times.
        """
        valid = [
            inst for inst in instruments
            if has_instrument_data(self.cfg.data_dir, inst)
        ]
        skipped = [inst for inst in instruments if inst not in valid]
        for inst in skipped:
            print(f"[MTFTrainer] Skipping {inst}: missing timeframe data")

        if not valid:
            raise RuntimeError("No instruments loaded for curriculum training")

        print(f"[MTFTrainer] Curriculum: {valid}")
        steps_per_instrument = max(1, self.cfg.total_steps // len(valid))

        n_envs = max(1, self.cfg.n_envs)
        vec = make_vec_env(
            [self._env_kwargs(valid[0]) for _ in range(n_envs)],
            start_method=self.cfg.vec_start_method,
        )
        try:
            for i, inst in enumerate(valid):
                print(f"\n[MTFTrainer] Training on {inst} ({steps_per_instrument:,} steps)")
                if i > 0:
                    vec.reload([self._env_kwargs(inst) for _ in range(n_envs)])
                target = self.global_step + steps_per_instrument
                self._run_loop(vec, target, label=f" ({inst})")

            self.save("curriculum_final")
            self._flush_log()
        finally:
            vec.close()
        print(f"[MTFTrainer] Curriculum complete. Best reward: {self.best_reward:.4f}")

    def _run_loop(self, vec_env, target_step: int, label: str = ""):
        """Collect parallel rollouts from *vec_env* and run PPO until *target_step*.

        ``self.global_step`` counts total environment transitions and advances by
        ``vec_env.num_envs`` per parallel step, so the wall-clock time to reach a
        given step budget shrinks ~``n_envs``×.
        """
        n = vec_env.num_envs
        buffer = MTFVecRolloutBuffer(
            self.cfg.rollout_steps, n, OBS_SHAPE, N_CONTEXT_FEATURES, self.device
        )

        obs, ctx, sym = vec_env.reset()  # arrays shape (n, ...)
        ep_rewards: list[float] = []
        ep_r = np.zeros(n, dtype=np.float64)
        last_info: list[dict] = [{} for _ in range(n)]

        t0 = time.time()
        start_step = self.global_step
        last_log = self.global_step
        last_save = self.global_step

        print(f"[MTFTrainer] Starting{label} — target {target_step:,} steps")

        while self.global_step < target_step:
            buffer.reset()
            self.agent.eval()

            for _ in range(self.cfg.rollout_steps):
                with torch.no_grad():
                    obs_t = torch.as_tensor(obs, dtype=torch.float32, device=self.device)
                    ctx_t = torch.as_tensor(ctx, dtype=torch.float32, device=self.device)
                    sym_t = torch.as_tensor(sym, dtype=torch.long, device=self.device)
                    logits, values = self.agent.act(obs_t, ctx_t, sym_t)
                    dist = Categorical(logits=logits)
                    actions = dist.sample()
                    log_probs = dist.log_prob(actions)

                actions_np = actions.cpu().numpy()
                next_obs, next_ctx, next_sym, rewards, dones, infos = vec_env.step(actions_np)

                buffer.add(
                    obs, ctx, sym,
                    actions_np, log_probs.cpu().numpy(),
                    rewards, values.cpu().numpy(), dones,
                )

                ep_r += rewards
                for i in range(n):
                    last_info[i] = infos[i]
                    if dones[i]:
                        ep_rewards.append(float(ep_r[i]))
                        ep_r[i] = 0.0

                obs, ctx, sym = next_obs, next_ctx, next_sym
                self.global_step += n

            with torch.no_grad():
                obs_t = torch.as_tensor(obs, dtype=torch.float32, device=self.device)
                ctx_t = torch.as_tensor(ctx, dtype=torch.float32, device=self.device)
                sym_t = torch.as_tensor(sym, dtype=torch.long, device=self.device)
                _, last_vals = self.agent.act(obs_t, ctx_t, sym_t)
            buffer.compute_returns(last_vals.cpu().numpy(), self.cfg.gamma, self.cfg.gae_lambda)

            metrics = self._update(buffer)

            if self.cfg.lr_anneal:
                frac = max(0.0, 1.0 - self.global_step / max(1, target_step))
                for g in self.opt.param_groups:
                    g["lr"] = self.cfg.lr * frac

            if self.global_step - last_log >= self.cfg.log_interval:
                last_log = self.global_step
                elapsed = time.time() - t0
                sps = (self.global_step - start_step) / elapsed if elapsed > 0 else 0
                mean_ep = np.mean(ep_rewards[-50:]) if ep_rewards else 0.0
                total_trades = sum(int(inf.get("n_trades", 0)) for inf in last_info)
                mean_bal = float(np.mean([inf.get("balance", 0.0) for inf in last_info]))
                entry = {
                    "step": self.global_step,
                    "mean_reward": round(float(mean_ep), 4),
                    "n_trades": total_trades,
                    "balance": round(mean_bal, 2),
                    "sps": round(sps, 0),
                    "n_envs": n,
                    **metrics,
                }
                self.log.append(entry)
                print(
                    f"[{self.global_step:>8,}] "
                    f"reward={mean_ep:+.3f}  "
                    f"trades={total_trades}  "
                    f"bal={mean_bal:,.0f}  "
                    f"sps={sps:.0f}  "
                    f"loss_p={metrics['policy_loss']:.4f}  "
                    f"loss_v={metrics['value_loss']:.4f}"
                )
                if mean_ep > self.best_reward and len(ep_rewards) >= 5:
                    self.best_reward = float(mean_ep)
                    self.save("best")

            if self.global_step - last_save >= self.cfg.save_interval:
                last_save = self.global_step
                self.save(f"step_{self.global_step}")
                self._flush_log()

    def _update(self, buffer) -> dict:
        self.agent.train()
        pg_losses, vf_losses, ent_losses, clip_fracs = [], [], [], []

        for _ in range(self.cfg.n_epochs):
            for obs_b, ctx_b, sym_b, act_b, old_lp_b, ret_b, adv_b in buffer.get_batches(
                self.cfg.batch_size
            ):
                logits, values = self.agent.act(obs_b, ctx_b, sym_b)
                dist = Categorical(logits=logits)
                new_lp = dist.log_prob(act_b)
                entropy = dist.entropy().mean()

                ratio = torch.exp(new_lp - old_lp_b)
                pg1 = -adv_b * ratio
                pg2 = -adv_b * torch.clamp(ratio, 1 - self.cfg.clip_eps, 1 + self.cfg.clip_eps)
                pg_loss = torch.max(pg1, pg2).mean()

                vf_loss = nn.functional.mse_loss(values, ret_b)

                loss = pg_loss + self.cfg.vf_coef * vf_loss - self.cfg.ent_coef * entropy

                self.opt.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(self.agent.parameters(), self.cfg.max_grad_norm)
                self.opt.step()

                clip_frac = ((ratio - 1).abs() > self.cfg.clip_eps).float().mean()
                pg_losses.append(float(pg_loss.detach()))
                vf_losses.append(float(vf_loss.detach()))
                ent_losses.append(float(entropy.detach()))
                clip_fracs.append(float(clip_frac.detach()))

        return {
            "policy_loss": round(np.mean(pg_losses), 6),
            "value_loss": round(np.mean(vf_losses), 6),
            "entropy": round(np.mean(ent_losses), 6),
            "clip_frac": round(np.mean(clip_fracs), 6),
        }

    def save(self, tag: str):
        path = Path(self.cfg.save_dir) / f"apex_rl_mtf_{tag}.pt"
        meta = {
            "obs_contract_version": OBS_CONTRACT_VERSION,
            "obs_schema_hash": schema_hash(),
            "obs_schema": schema(),
            "n_features": OBS_FEATURES,
            "context_dim": N_CONTEXT_FEATURES,
            "n_symbols": len(self._vocab),
            "symbol_vocab": self._vocab,
        }
        torch.save(
            {
                "step": self.global_step,
                "agent": self.agent.state_dict(),
                "optimizer": self.opt.state_dict(),
                "best_reward": self.best_reward,
                "cfg": asdict(self.cfg),
                "meta": meta,
            },
            path,
        )

    def load(self, path: str):
        ckpt = torch.load(path, map_location=self.device)
        self.agent.load_state_dict(ckpt["agent"])
        self.opt.load_state_dict(ckpt["optimizer"])
        self.global_step = ckpt["step"]
        self.best_reward = ckpt["best_reward"]
        print(f"[MTFTrainer] Loaded checkpoint: step={self.global_step}")

    def _flush_log(self):
        with open(self.cfg.log_path, "w") as f:
            json.dump(self.log, f, indent=2)
