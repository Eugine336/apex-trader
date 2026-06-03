"""
APEX RL — Colab Training Script
=================================
Run this on Google Colab (free T4 GPU).

Step 1: Upload your OHLCV CSV to Colab
Step 2: Run this script
Step 3: Download the checkpoint when done
Step 4: Drop checkpoint into APEX and enable RLBridge

Zero cost. Free T4 GPU gives ~8-15 hours per session.
2 million steps on H1 data takes roughly 4-6 hours.
"""

# ─────────────────────────────────────────────────────────────────────────────
# CELL 1 — Install dependencies
# ─────────────────────────────────────────────────────────────────────────────
# !pip install torch numpy pandas -q

# ─────────────────────────────────────────────────────────────────────────────
# CELL 2 — Mount Drive (optional, to save checkpoints persistently)
# ─────────────────────────────────────────────────────────────────────────────
# from google.colab import drive
# drive.mount('/content/drive')
# SAVE_DIR = "/content/drive/MyDrive/apex_rl/checkpoints"

# ─────────────────────────────────────────────────────────────────────────────
# CELL 3 — Upload your CSV
# ─────────────────────────────────────────────────────────────────────────────
# from google.colab import files
# uploaded = files.upload()  # upload EURUSD_H1.csv

# ─────────────────────────────────────────────────────────────────────────────
# CELL 4 — Training
# ─────────────────────────────────────────────────────────────────────────────

import sys
import os

# If running from Colab, clone or upload the rl/ package first
# sys.path.insert(0, '/content/apex_rl')

from rl.trainer import PPOTrainer, PPOConfig

# ── Configure ────────────────────────────────────────────────────────────────

CSV_PATH  = "EURUSD_H1.csv"     # change to your uploaded file
PIP_SIZE  = 0.0001              # forex major pairs
SAVE_DIR  = "checkpoints"

cfg = PPOConfig(
    # Rollout
    rollout_steps  = 2048,
    n_epochs       = 10,
    batch_size     = 256,

    # PPO
    clip_eps       = 0.2,
    vf_coef        = 0.5,
    ent_coef       = 0.01,       # increase if agent stops exploring

    # Learning
    lr             = 3e-4,
    lr_anneal      = True,

    # Discount
    gamma          = 0.99,
    gae_lambda     = 0.95,

    # Duration — 2M steps is a good first run
    # Increase to 5M+ for more emergence
    total_steps    = 2_000_000,

    # Logging
    log_interval   = 10_000,
    save_interval  = 200_000,
    eval_interval  = 50_000,

    save_dir       = SAVE_DIR,
    log_path       = "training_log.json",
)

# ── Train ────────────────────────────────────────────────────────────────────

trainer = PPOTrainer(cfg, csv_path=CSV_PATH, pip_size=PIP_SIZE)
trainer.train()

# ── Download checkpoint ───────────────────────────────────────────────────────
# from google.colab import files
# files.download(f"{SAVE_DIR}/apex_rl_best.pt")

# ─────────────────────────────────────────────────────────────────────────────
# CELL 5 — Inspect training log
# ─────────────────────────────────────────────────────────────────────────────

import json
import numpy as np

with open("training_log.json") as f:
    log = json.load(f)

if log:
    steps    = [e["step"] for e in log]
    rewards  = [e["mean_reward"] for e in log]
    balances = [e["balance"] for e in log]

    print(f"Total log entries : {len(log)}")
    print(f"Final mean reward : {rewards[-1]:.4f}")
    print(f"Best mean reward  : {max(rewards):.4f}")
    print(f"Final balance     : ${balances[-1]:,.2f}")
    print(f"Return            : {(balances[-1] - 10000) / 10000 * 100:.1f}%")

    # Emergence signal: is reward trending up?
    first_half = np.mean(rewards[:len(rewards)//2])
    second_half = np.mean(rewards[len(rewards)//2:])
    trend = "↑ IMPROVING" if second_half > first_half else "↓ NOT YET CONVERGED"
    print(f"\nReward trend: {trend}")
    print(f"  First half avg:  {first_half:.4f}")
    print(f"  Second half avg: {second_half:.4f}")

# ─────────────────────────────────────────────────────────────────────────────
# CELL 6 — Quick inference test
# ─────────────────────────────────────────────────────────────────────────────

import numpy as np
import torch
from rl.network import ApexRLAgent, ACTION_LABELS

agent = ApexRLAgent()
ckpt  = torch.load(f"{SAVE_DIR}/apex_rl_best.pt", map_location="cpu")
agent.load_state_dict(ckpt["agent"])
agent.eval()

# Dummy observation — replace with real obs from ObservationBuilder
obs = np.random.randn(50, 12).astype(np.float32)
action, confidence, expected_r = agent.predict(obs)

print(f"\nInference test:")
print(f"  Action     : {ACTION_LABELS[action]}")
print(f"  Confidence : {confidence:.3f}")
print(f"  Expected R : {expected_r:.3f}")
print(f"\nAgent trained at step: {ckpt['step']:,}")
print(f"Best reward achieved : {ckpt['best_reward']:.4f}")

# ─────────────────────────────────────────────────────────────────────────────
# NOTES ON EMERGENCE
# ─────────────────────────────────────────────────────────────────────────────
#
# What to look for:
#   - Mean reward trending positive over training
#   - Clip fraction staying below 0.3 (stable updates)
#   - Entropy not collapsing to zero (agent still exploring)
#   - Balance growing in simulation
#
# What emergence looks like:
#   - Agent starts HOLDing during low-volatility periods
#   - Agent starts avoiding certain sessions
#   - Agent develops asymmetric sizing behaviour
#   - Agent learns to exit early before known reversal patterns
#
# None of these behaviours are programmed.
# They appear because they increase reward.
# That is the emergence.
#
# First training run often produces marginal results.
# Iterate: adjust reward shaping, run longer, try multiple timeframes.
# The system improves with each iteration.
# ─────────────────────────────────────────────────────────────────────────────
