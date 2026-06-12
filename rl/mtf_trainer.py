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
    batch_size: int = 256

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
        self.buffer = MTFRolloutBuffer(
            cfg.rollout_steps,
            OBS_SHAPE,
            N_CONTEXT_FEATURES,
            self.device,
        )

        Path(cfg.save_dir).mkdir(exist_ok=True)
        self.log: list[dict] = []
        self.global_step = 0
        self.best_reward = -np.inf

    def train(self):
        obs, ctx, sym_id = self.env.reset()
        ep_rewards: list[float] = []
        ep_r = 0.0
        t0 = time.time()

        print(f"[MTFTrainer] Starting — target {self.cfg.total_steps:,} steps")

        while self.global_step < self.cfg.total_steps:
            self.buffer.reset()
            self.agent.eval()

            for _ in range(self.cfg.rollout_steps):
                with torch.no_grad():
                    obs_t = torch.FloatTensor(obs).unsqueeze(0).to(self.device)
                    ctx_t = torch.FloatTensor(ctx).unsqueeze(0).to(self.device)
                    sym_t = torch.LongTensor([sym_id]).to(self.device)
                    logits, value = self.agent.act(obs_t, ctx_t, sym_t)
                    dist = Categorical(logits=logits)
                    action = dist.sample()
                    lp = dist.log_prob(action)

                (next_obs, next_ctx, next_sym), reward, done, info = self.env.step(int(action.item()))
                ep_r += reward

                self.buffer.add(
                    obs, ctx, sym_id,
                    int(action.item()), float(lp.item()),
                    reward, float(value.item()), float(done),
                )

                obs, ctx, sym_id = next_obs, next_ctx, next_sym
                self.global_step += 1

                if done:
                    ep_rewards.append(ep_r)
                    ep_r = 0.0
                    obs, ctx, sym_id = self.env.reset()

            with torch.no_grad():
                obs_t = torch.FloatTensor(obs).unsqueeze(0).to(self.device)
                ctx_t = torch.FloatTensor(ctx).unsqueeze(0).to(self.device)
                sym_t = torch.LongTensor([sym_id]).to(self.device)
                _, last_val = self.agent.act(obs_t, ctx_t, sym_t)
            self.buffer.compute_returns(float(last_val.item()), self.cfg.gamma, self.cfg.gae_lambda)

            metrics = self._update()

            if self.cfg.lr_anneal:
                frac = 1.0 - self.global_step / self.cfg.total_steps
                for g in self.opt.param_groups:
                    g["lr"] = self.cfg.lr * frac

            if self.global_step % self.cfg.log_interval < self.cfg.rollout_steps:
                elapsed = time.time() - t0
                sps = self.global_step / elapsed if elapsed > 0 else 0
                mean_ep = np.mean(ep_rewards[-50:]) if ep_rewards else 0.0
                entry = {
                    "step": self.global_step,
                    "mean_reward": round(float(mean_ep), 4),
                    "n_trades": len(self.env.trades_log),
                    "balance": round(self.env.balance, 2),
                    "sps": round(sps, 0),
                    **metrics,
                }
                self.log.append(entry)
                print(
                    f"[{self.global_step:>8,}] "
                    f"reward={mean_ep:+.3f}  "
                    f"trades={len(self.env.trades_log)}  "
                    f"bal={self.env.balance:,.0f}  "
                    f"sps={sps:.0f}  "
                    f"loss_p={metrics['policy_loss']:.4f}  "
                    f"loss_v={metrics['value_loss']:.4f}"
                )
                if mean_ep > self.best_reward and len(ep_rewards) >= 5:
                    self.best_reward = float(mean_ep)
                    self.save("best")

            if self.global_step % self.cfg.save_interval < self.cfg.rollout_steps:
                self.save(f"step_{self.global_step}")
                self._flush_log()

        self.save("final")
        self._flush_log()
        print(f"[MTFTrainer] Training complete. Best reward: {self.best_reward:.4f}")

    def train_curriculum(self, instruments: list[str]):
        """Round-robin curriculum: one epoch per instrument, shared agent."""
        envs: dict[str, ApexMultiTFTradingEnv] = {}
        for inst in instruments:
            try:
                envs[inst] = ApexMultiTFTradingEnv(
                    data_dir=self.cfg.data_dir,
                    instrument=inst,
                    commission_per_lot=self.cfg.commission_per_lot,
                    slippage_factor=self.cfg.slippage_factor,
                )
                if self.cfg.reward_shaping:
                    envs[inst]._reward_shaping = self.cfg.reward_shaping
            except Exception as e:
                print(f"[MTFTrainer] Skipping {inst}: {e}")
                continue

        if not envs:
            raise RuntimeError("No instruments loaded for curriculum training")

        print(f"[MTFTrainer] Curriculum: {list(envs.keys())}")
        steps_per_instrument = max(1, self.cfg.total_steps // len(envs))

        for inst, env in envs.items():
            print(f"\n[MTFTrainer] Training on {inst} ({steps_per_instrument:,} steps)")
            self.env = env
            saved_total = self.cfg.total_steps
            self.cfg.total_steps = self.global_step + steps_per_instrument
            self.train()
            self.cfg.total_steps = saved_total

        self.save("curriculum_final")
        self._flush_log()
        print(f"[MTFTrainer] Curriculum complete. Best reward: {self.best_reward:.4f}")

    def _update(self) -> dict:
        self.agent.train()
        pg_losses, vf_losses, ent_losses, clip_fracs = [], [], [], []

        for _ in range(self.cfg.n_epochs):
            for obs_b, ctx_b, sym_b, act_b, old_lp_b, ret_b, adv_b in self.buffer.get_batches(
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
