"""
APEX RL — PPO Trainer
======================
Proximal Policy Optimization training loop.

Why PPO:
  - Stable training — clipped objective prevents catastrophic updates
  - Sample efficient — reuses experience across multiple epochs
  - Battle-tested — same algorithm used in ChatGPT's RLHF training
  - Runs well on free Colab T4 GPU

What this does:
  - Collects experience by running the agent in the environment
  - Computes advantages (was this action better or worse than expected?)
  - Updates encoder + policy + value head via clipped gradient descent
  - Logs everything for analysis

The emergence happens here — in the weight updates driven by reward.
Nobody programs what patterns to find. Profitable patterns get reinforced.
Unprofitable ones die. Over millions of steps, capability emerges.
"""

from __future__ import annotations

import os
import time
import json
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.distributions import Categorical
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from .environment import ApexTradingEnv
from .network import ApexRLAgent, ACTION_LABELS
from .contracts import OBS_CONTRACT_VERSION, schema_hash, schema


# ── Hyperparameters ───────────────────────────────────────────────────────────

@dataclass
class PPOConfig:
    # Environment
    window:          int   = 50
    n_envs:          int   = 1       # parallel envs (increase on Colab)

    # Rollout
    rollout_steps:   int   = 2048    # steps per rollout before update
    n_epochs:        int   = 10      # PPO epochs per rollout
    batch_size:      int   = 256

    # PPO
    clip_eps:        float = 0.2     # clipping epsilon
    vf_coef:         float = 0.5     # value loss coefficient
    ent_coef:        float = 0.01    # entropy bonus — encourages exploration
    max_grad_norm:   float = 0.5

    # Optimiser
    lr:              float = 3e-4
    lr_anneal:       bool  = True    # decay LR to 0 over training

    # Discount
    gamma:           float = 0.99
    gae_lambda:      float = 0.95    # GAE lambda

    # Training
    total_steps:     int   = 2_000_000
    eval_interval:   int   = 50_000
    save_interval:   int   = 100_000
    log_interval:    int   = 10_000

    # Paths
    save_dir:        str   = "checkpoints"
    log_path:        str   = "training_log.json"


# ── Rollout Buffer ────────────────────────────────────────────────────────────

class RolloutBuffer:
    """Stores experience from one rollout, computes GAE advantages."""

    def __init__(self, steps: int, obs_shape: tuple, device: torch.device):
        self.steps     = steps
        self.device    = device
        self.obs       = torch.zeros(steps, *obs_shape)
        self.actions   = torch.zeros(steps, dtype=torch.long)
        self.log_probs = torch.zeros(steps)
        self.rewards   = torch.zeros(steps)
        self.values    = torch.zeros(steps)
        self.dones     = torch.zeros(steps)
        self.ptr       = 0

    def add(self, obs, action, log_prob, reward, value, done):
        self.obs[self.ptr]       = torch.FloatTensor(obs)
        self.actions[self.ptr]   = action
        self.log_probs[self.ptr] = log_prob
        self.rewards[self.ptr]   = reward
        self.values[self.ptr]    = value
        self.dones[self.ptr]     = done
        self.ptr += 1

    def compute_returns(self, last_value: float, gamma: float, gae_lambda: float):
        """GAE advantage estimation."""
        advantages = torch.zeros(self.steps)
        last_gae   = 0.0

        for t in reversed(range(self.steps)):
            if t == self.steps - 1:
                next_val   = last_value
                next_done  = 0.0
            else:
                next_val   = float(self.values[t + 1])
                next_done  = float(self.dones[t + 1])

            delta    = (self.rewards[t]
                        + gamma * next_val * (1 - next_done)
                        - self.values[t])
            last_gae = delta + gamma * gae_lambda * (1 - next_done) * last_gae
            advantages[t] = last_gae

        self.returns    = advantages + self.values
        self.advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

    def get_batches(self, batch_size: int):
        """Yield random mini-batches."""
        idx = torch.randperm(self.steps)
        for start in range(0, self.steps, batch_size):
            b = idx[start:start + batch_size]
            yield (
                self.obs[b].to(self.device),
                self.actions[b].to(self.device),
                self.log_probs[b].to(self.device),
                self.returns[b].to(self.device),
                self.advantages[b].to(self.device),
            )

    def reset(self):
        self.ptr = 0


# ── Trainer ───────────────────────────────────────────────────────────────────

class PPOTrainer:
    """
    Drives the emergence loop.

    Usage:
        trainer = PPOTrainer(cfg, csv_path="data/EURUSD_H1.csv")
        trainer.train()

    After training, use trainer.agent.predict(obs) for inference.
    """

    def __init__(self, cfg: PPOConfig, csv_path: str, pip_size: float = 0.0001):
        self.cfg    = cfg
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        print(f"[Trainer] Device: {self.device}")

        self.env   = ApexTradingEnv(csv_path, pip_size=pip_size)
        self.agent = ApexRLAgent(
            n_features=self.env.N_FEATURES,
            n_actions=self.env.action_space_n(),
        ).to(self.device)

        print(f"[Trainer] Agent parameters: {self.agent.count_parameters():,}")

        self.opt    = optim.Adam(self.agent.parameters(), lr=cfg.lr, eps=1e-5)
        self.buffer = RolloutBuffer(
            cfg.rollout_steps,
            self.env.observation_shape(),
            self.device,
        )

        Path(cfg.save_dir).mkdir(exist_ok=True)
        self.log: list[dict] = []
        self.global_step = 0
        self.best_reward = -np.inf

    # ── Main training loop ────────────────────────────────────────────────

    def train(self):
        obs  = self.env.reset()
        ep_rewards: list[float] = []
        ep_r = 0.0
        t0   = time.time()

        print(f"[Trainer] Starting training — target {self.cfg.total_steps:,} steps")

        while self.global_step < self.cfg.total_steps:

            # ── Collect rollout ───────────────────────────────────────────
            self.buffer.reset()
            self.agent.eval()

            for _ in range(self.cfg.rollout_steps):
                with torch.no_grad():
                    obs_t  = torch.FloatTensor(obs).unsqueeze(0).to(self.device)
                    logits, value = self.agent.act(obs_t)
                    dist   = Categorical(logits=logits)
                    action = dist.sample()
                    lp     = dist.log_prob(action)

                next_obs, reward, done, info = self.env.step(int(action.item()))
                ep_r += reward

                self.buffer.add(
                    obs, int(action.item()), float(lp.item()),
                    reward, float(value.item()), float(done),
                )

                obs = next_obs
                self.global_step += 1

                if done:
                    ep_rewards.append(ep_r)
                    ep_r = 0.0
                    obs  = self.env.reset()

            # ── Compute returns ───────────────────────────────────────────
            with torch.no_grad():
                obs_t = torch.FloatTensor(obs).unsqueeze(0).to(self.device)
                _, last_val = self.agent.act(obs_t)
            self.buffer.compute_returns(
                float(last_val.item()), self.cfg.gamma, self.cfg.gae_lambda
            )

            # ── PPO update ────────────────────────────────────────────────
            metrics = self._update()

            # ── LR annealing ──────────────────────────────────────────────
            if self.cfg.lr_anneal:
                frac = 1.0 - self.global_step / self.cfg.total_steps
                for g in self.opt.param_groups:
                    g["lr"] = self.cfg.lr * frac

            # ── Logging ───────────────────────────────────────────────────
            if self.global_step % self.cfg.log_interval < self.cfg.rollout_steps:
                elapsed = time.time() - t0
                sps     = self.global_step / elapsed
                mean_ep = np.mean(ep_rewards[-50:]) if ep_rewards else 0.0

                entry = {
                    "step":        self.global_step,
                    "mean_reward": round(mean_ep, 4),
                    "n_trades":    len(self.env.trades_log),
                    "balance":     round(self.env.balance, 2),
                    "sps":         round(sps, 0),
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
                    self.best_reward = mean_ep
                    self.save("best")

            # ── Periodic save ─────────────────────────────────────────────
            if self.global_step % self.cfg.save_interval < self.cfg.rollout_steps:
                self.save(f"step_{self.global_step}")
                self._flush_log()

        self.save("final")
        self._flush_log()
        print(f"[Trainer] Training complete. Best reward: {self.best_reward:.4f}")

    # ── PPO Update ────────────────────────────────────────────────────────

    def _update(self) -> dict:
        self.agent.train()
        pg_losses, vf_losses, ent_losses, clip_fracs = [], [], [], []

        for _ in range(self.cfg.n_epochs):
            for obs_b, act_b, old_lp_b, ret_b, adv_b in self.buffer.get_batches(
                self.cfg.batch_size
            ):
                logits, values = self.agent.act(obs_b)
                dist    = Categorical(logits=logits)
                new_lp  = dist.log_prob(act_b)
                entropy = dist.entropy().mean()

                # Policy loss — clipped PPO objective
                ratio   = torch.exp(new_lp - old_lp_b)
                pg1     = -adv_b * ratio
                pg2     = -adv_b * torch.clamp(ratio, 1 - self.cfg.clip_eps,
                                                       1 + self.cfg.clip_eps)
                pg_loss = torch.max(pg1, pg2).mean()

                # Value loss
                vf_loss = F.mse_loss(values, ret_b)

                # Total loss
                loss = (pg_loss
                        + self.cfg.vf_coef * vf_loss
                        - self.cfg.ent_coef * entropy)

                self.opt.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(self.agent.parameters(),
                                         self.cfg.max_grad_norm)
                self.opt.step()

                clip_frac = ((ratio - 1).abs() > self.cfg.clip_eps).float().mean()
                pg_losses.append(float(pg_loss))
                vf_losses.append(float(vf_loss))
                ent_losses.append(float(entropy))
                clip_fracs.append(float(clip_frac))

        return {
            "policy_loss":  round(np.mean(pg_losses), 6),
            "value_loss":   round(np.mean(vf_losses), 6),
            "entropy":      round(np.mean(ent_losses), 6),
            "clip_frac":    round(np.mean(clip_fracs), 6),
        }

    # ── Persistence ───────────────────────────────────────────────────────

    def save(self, tag: str):
        path = Path(self.cfg.save_dir) / f"apex_rl_{tag}.pt"
        meta = {
            "obs_contract_version": OBS_CONTRACT_VERSION,
            "obs_schema_hash": schema_hash(),
            "obs_schema": schema(),
            "n_features": self.env.N_FEATURES,
            "context_dim": self.agent.context_dim,
            "n_symbols": self.agent.n_symbols,
        }
        torch.save({
            "step":          self.global_step,
            "agent":         self.agent.state_dict(),
            "optimizer":     self.opt.state_dict(),
            "best_reward":   self.best_reward,
            "cfg":           asdict(self.cfg),
            "meta":          meta,
        }, path)

    def load(self, path: str):
        ckpt = torch.load(path, map_location=self.device)
        self.agent.load_state_dict(ckpt["agent"])
        self.opt.load_state_dict(ckpt["optimizer"])
        self.global_step = ckpt["step"]
        self.best_reward = ckpt["best_reward"]
        print(f"[Trainer] Loaded checkpoint: step={self.global_step}")

    def _flush_log(self):
        with open(self.cfg.log_path, "w") as f:
            json.dump(self.log, f, indent=2)
