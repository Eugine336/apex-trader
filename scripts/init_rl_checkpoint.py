"""
APEX RL — Initialise a fresh MTF checkpoint
=============================================
Creates a random-weight checkpoint whose architecture and schema metadata
match the live ``rl.contracts`` spec so ``RLBridge`` / ``ShadowEngine``
can load it for shadow-only operation.

Usage::

    python scripts/init_rl_checkpoint.py [--out checkpoints/apex_rl_best.pt]

The produced ``.pt`` file is gitignored (``checkpoints/*.pt``).
Do NOT commit it — store via LFS or object storage for production.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import torch
from rl.contracts import (
    OBS_CONTRACT_VERSION,
    OBS_FEATURES,
    N_CONTEXT_FEATURES,
    build_symbol_vocab,
    schema_hash,
    schema,
)
from rl.network import ApexRLAgent


def main(out: str = "checkpoints/apex_rl_best.pt") -> None:
    vocab = build_symbol_vocab()

    agent = ApexRLAgent(
        n_features=OBS_FEATURES,
        n_actions=4,
        context_dim=N_CONTEXT_FEATURES,
        n_symbols=len(vocab),
    )

    meta = {
        "obs_contract_version": OBS_CONTRACT_VERSION,
        "obs_schema_hash": schema_hash(),
        "obs_schema": schema(),
        "n_features": OBS_FEATURES,
        "context_dim": N_CONTEXT_FEATURES,
        "n_symbols": len(vocab),
        "symbol_vocab": vocab,
        "initialized_only": True,
    }

    ckpt = {
        "agent": agent.state_dict(),
        "step": 0,
        "meta": meta,
    }

    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(ckpt, path)

    print(f"Checkpoint saved: {path}")
    print(f"  version       : {OBS_CONTRACT_VERSION}")
    print(f"  schema_hash   : {schema_hash()}")
    print(f"  n_features    : {OBS_FEATURES}")
    print(f"  context_dim   : {N_CONTEXT_FEATURES}")
    print(f"  n_symbols     : {len(vocab)}")
    print(f"  parameters    : {agent.count_parameters():,}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialise APEX RL checkpoint")
    parser.add_argument("--out", default="checkpoints/apex_rl_best.pt")
    args = parser.parse_args()
    main(args.out)
