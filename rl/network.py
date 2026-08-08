"""
APEX RL — Neural Architecture
==============================
Three components:

1. MarketEncoder
   Temporal convolutional network that reads raw OHLCV windows
   and learns its own market representations.
   Nobody tells it what an order block is.
   Nobody tells it what a FVG is.
   It discovers latent structure from reward.

2. PolicyHead
   Takes encoder output → probability distribution over actions.
   HOLD / BUY / SELL / CLOSE

3. ValueHead
   Takes encoder output → estimated future R-multiple.
   This becomes the confidence/expectancy signal fed to APEX scanner.

Together: Actor-Critic architecture (PPO-compatible).

Zero external ML dependencies beyond PyTorch.
Runs on CPU for inference, GPU for training on Colab.
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np


# ── Hyperparameters ───────────────────────────────────────────────────────────

ENCODER_CHANNELS = [64, 128, 128]   # TCN channel progression
ENCODER_KERNELS  = [3, 3, 3]        # kernel sizes per layer
LATENT_DIM       = 256              # size of learned market representation
POLICY_HIDDEN    = 128
VALUE_HIDDEN     = 128
DROPOUT          = 0.1


# ── Encoder ───────────────────────────────────────────────────────────────────

class CausalConv1d(nn.Module):
    """
    Causal convolution — only looks at past candles, never the future.
    Critical for avoiding data leakage in temporal models.
    """
    def __init__(self, in_ch: int, out_ch: int, kernel: int, dilation: int = 1):
        super().__init__()
        self.pad  = (kernel - 1) * dilation
        self.conv = nn.Conv1d(
            in_ch, out_ch, kernel,
            padding=self.pad,
            dilation=dilation,
            bias=False,
        )
        self.bn   = nn.BatchNorm1d(out_ch)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (batch, channels, time)
        out = self.conv(x)
        out = out[:, :, :-self.pad] if self.pad > 0 else out
        return F.gelu(self.bn(out))


class ResidualBlock(nn.Module):
    """Residual connection for stable deep training."""
    def __init__(self, channels: int, kernel: int, dilation: int):
        super().__init__()
        self.c1 = CausalConv1d(channels, channels, kernel, dilation)
        self.c2 = CausalConv1d(channels, channels, kernel, dilation)
        self.drop = nn.Dropout(DROPOUT)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + self.drop(self.c2(self.c1(x)))


class MarketEncoder(nn.Module):
    """
    Temporal Convolutional Network that learns market representations.

    Input:  (batch, window, n_features)  — e.g. (32, 50, 12)
    Output: (batch, LATENT_DIM)          — learned market state vector

    The latent vector is the emergence vector.
    It encodes whatever the model found useful for predicting reward.
    We do not control what it learns to represent.
    """

    def __init__(self, n_features: int = 12, latent_dim: int = LATENT_DIM):
        super().__init__()
        self.latent_dim = latent_dim

        layers = []
        in_ch  = n_features

        for i, (out_ch, k) in enumerate(zip(ENCODER_CHANNELS, ENCODER_KERNELS)):
            dilation = 2 ** i
            layers.append(CausalConv1d(in_ch, out_ch, k, dilation))
            layers.append(ResidualBlock(out_ch, k, dilation))
            in_ch = out_ch

        self.tcn = nn.Sequential(*layers)

        # Multi-scale attention pooling — lets the model weight
        # which time steps matter most for the current decision
        self.attn = nn.Linear(ENCODER_CHANNELS[-1], 1)

        self.proj = nn.Sequential(
            nn.Linear(ENCODER_CHANNELS[-1], latent_dim),
            nn.LayerNorm(latent_dim),
            nn.GELU(),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (batch, window, features) → (batch, features, window)
        x = x.permute(0, 2, 1)
        x = self.tcn(x)
        # x: (batch, channels, window)

        # Attention pooling over time
        x_t = x.permute(0, 2, 1)              # (batch, window, channels)
        weights = F.softmax(self.attn(x_t), dim=1)   # (batch, window, 1)
        pooled  = (x_t * weights).sum(dim=1)  # (batch, channels)

        return self.proj(pooled)              # (batch, latent_dim)


# ── Policy Head ───────────────────────────────────────────────────────────────

class PolicyHead(nn.Module):
    """
    Maps latent market state → action distribution.
    Actions: 0=HOLD, 1=BUY, 2=SELL, 3=CLOSE

    Returns logits (unnormalised). Apply softmax externally or
    use Categorical distribution for sampling.
    """

    def __init__(self, latent_dim: int = LATENT_DIM, n_actions: int = 4):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(latent_dim, POLICY_HIDDEN),
            nn.LayerNorm(POLICY_HIDDEN),
            nn.GELU(),
            nn.Dropout(DROPOUT),
            nn.Linear(POLICY_HIDDEN, POLICY_HIDDEN // 2),
            nn.GELU(),
            nn.Linear(POLICY_HIDDEN // 2, n_actions),
        )

    def forward(self, latent: torch.Tensor) -> torch.Tensor:
        return self.net(latent)   # (batch, n_actions) — raw logits


# ── Value Head ────────────────────────────────────────────────────────────────

class ValueHead(nn.Module):
    """
    Maps latent market state → scalar expected R-multiple.

    This is the signal that eventually feeds the APEX scanner as
    a confidence/expectancy estimate. Stage 3 of the authority model.
    """

    def __init__(self, latent_dim: int = LATENT_DIM):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(latent_dim, VALUE_HIDDEN),
            nn.LayerNorm(VALUE_HIDDEN),
            nn.GELU(),
            nn.Dropout(DROPOUT),
            nn.Linear(VALUE_HIDDEN, VALUE_HIDDEN // 2),
            nn.GELU(),
            nn.Linear(VALUE_HIDDEN // 2, 1),
        )

    def forward(self, latent: torch.Tensor) -> torch.Tensor:
        return self.net(latent).squeeze(-1)   # (batch,) — expected R


# ── Full Actor-Critic Model ───────────────────────────────────────────────────

CONTEXT_EMBED_DIM = 16
CONTEXT_PROJ_DIM  = 32


class ApexRLAgent(nn.Module):
    """
    Complete actor-critic agent.

    encode()  → latent market representation
    act()     → action logits + value estimate
    predict() → inference only, no grad

    The encoder's learned representations are the emergence.
    The policy discovers what to do with those representations.
    The value head estimates expected R — this becomes the APEX signal.

    When ``context_dim > 0`` the agent becomes a **symbol-conditioned
    generalist**: a learned symbol embedding + context MLP is concatenated
    to the latent before the policy/value heads.  When ``context_dim == 0``
    (the default) the agent is byte-identical to the original 12-feature
    single-instrument architecture.
    """

    def __init__(
        self,
        n_features: int = 12,
        n_actions: int = 4,
        context_dim: int = 0,
        n_symbols: int = 0,
    ):
        super().__init__()
        self.context_dim = context_dim
        self.n_symbols   = n_symbols

        self.encoder = MarketEncoder(n_features, LATENT_DIM)

        if context_dim > 0:
            self.symbol_embed = nn.Embedding(max(n_symbols, 1), CONTEXT_EMBED_DIM) if n_symbols > 0 else None
            raw_ctx_dim = context_dim + CONTEXT_EMBED_DIM if n_symbols > 0 else context_dim
            self.context_proj = nn.Sequential(
                nn.Linear(raw_ctx_dim, CONTEXT_PROJ_DIM),
                nn.LayerNorm(CONTEXT_PROJ_DIM),
                nn.GELU(),
            )
            head_input = LATENT_DIM + CONTEXT_PROJ_DIM
        else:
            self.symbol_embed = None
            self.context_proj = None
            head_input = LATENT_DIM

        self.policy = PolicyHead(head_input, n_actions)
        self.value  = ValueHead(head_input)

        self._init_weights()

    def _init_weights(self):
        for m in self.modules():
            if isinstance(m, (nn.Linear, nn.Conv1d)):
                nn.init.orthogonal_(m.weight, gain=np.sqrt(2))
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
        nn.init.orthogonal_(self.policy.net[-1].weight, gain=0.01)
        nn.init.orthogonal_(self.value.net[-1].weight,  gain=1.0)

    def _fuse_context(
        self,
        latent: torch.Tensor,
        context_vec: torch.Tensor | None = None,
        symbol_id: torch.Tensor | None = None,
    ) -> torch.Tensor:
        """Concatenate context projection to the latent when context is active."""
        if self.context_dim == 0 or self.context_proj is None:
            return latent

        if context_vec is None:
            context_vec = torch.zeros(latent.shape[0], self.context_dim, device=latent.device)

        parts: list[torch.Tensor] = []
        if self.symbol_embed is not None and symbol_id is not None:
            # Guard against out-of-range symbol ids (e.g. a symbol not in the
            # training vocab, or a hash-derived id from a missing universe).
            # An invalid id would otherwise raise "index out of range in self"
            # inside nn.Embedding. Treat unknown symbols as a zero embedding,
            # identical to the "no symbol_id" path below.
            ids = symbol_id.long()
            n_embed = self.symbol_embed.num_embeddings
            valid = (ids >= 0) & (ids < n_embed)
            safe_ids = torch.where(valid, ids, torch.zeros_like(ids))
            sym_emb = self.symbol_embed(safe_ids)
            if not bool(valid.all()):
                sym_emb = sym_emb * valid.unsqueeze(-1).to(sym_emb.dtype)
            parts = [context_vec, sym_emb]
        elif self.symbol_embed is not None:
            parts = [context_vec, torch.zeros(latent.shape[0], CONTEXT_EMBED_DIM, device=latent.device)]
        else:
            parts = [context_vec]

        ctx_input = torch.cat(parts, dim=-1)
        ctx_proj  = self.context_proj(ctx_input)
        return torch.cat([latent, ctx_proj], dim=-1)

    def encode(self, obs: torch.Tensor) -> torch.Tensor:
        """Return learned market representation."""
        return self.encoder(obs)

    def act(
        self,
        obs: torch.Tensor,
        context_vec: torch.Tensor | None = None,
        symbol_id: torch.Tensor | None = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Return (action_logits, value_estimate)."""
        latent = self._fuse_context(self.encoder(obs), context_vec, symbol_id)
        logits = self.policy(latent)
        val    = self.value(latent)
        return logits, val

    @torch.no_grad()
    def predict(
        self,
        obs: np.ndarray,
        context_vec: np.ndarray | None = None,
        symbol_id: int | None = None,
    ) -> tuple[int, float, float]:
        """
        Inference — used during shadow trading and live signal generation.

        Returns:
            action     : int   — 0=HOLD, 1=BUY, 2=SELL, 3=CLOSE
            confidence : float — probability of chosen action
            expected_r : float — estimated R-multiple (the APEX signal)
        """
        self.eval()
        device = next(self.parameters()).device
        t = torch.as_tensor(obs, dtype=torch.float32, device=device).unsqueeze(0)

        ctx_t = None
        sym_t = None
        if context_vec is not None:
            ctx_t = torch.as_tensor(context_vec, dtype=torch.float32, device=device).unsqueeze(0)
        if symbol_id is not None:
            sym_t = torch.as_tensor([symbol_id], dtype=torch.long, device=device)

        latent = self._fuse_context(self.encoder(t), ctx_t, sym_t)
        logits = self.policy(latent)
        val    = self.value(latent)

        probs  = F.softmax(logits, dim=-1).squeeze(0)
        action = int(probs.argmax().item())
        conf   = float(probs[action].item())
        exp_r  = float(val.item())

        return action, conf, exp_r

    @torch.no_grad()
    def predict_full(
        self,
        obs: np.ndarray,
        context_vec: np.ndarray | None = None,
        symbol_id: int | None = None,
    ) -> tuple[int, float, float, list]:
        """
        Inference with latent — single forward pass.

        Returns:
            action     : int   — 0=HOLD, 1=BUY, 2=SELL, 3=CLOSE
            confidence : float — probability of chosen action
            expected_r : float — estimated R-multiple
            latent_list: list  — learned market representation
        """
        self.eval()
        device = next(self.parameters()).device
        t = torch.as_tensor(obs, dtype=torch.float32, device=device).unsqueeze(0)

        ctx_t = None
        sym_t = None
        if context_vec is not None:
            ctx_t = torch.as_tensor(context_vec, dtype=torch.float32, device=device).unsqueeze(0)
        if symbol_id is not None:
            sym_t = torch.as_tensor([symbol_id], dtype=torch.long, device=device)

        enc_out = self.encoder(t)
        latent  = self._fuse_context(enc_out, ctx_t, sym_t)
        logits  = self.policy(latent)
        val     = self.value(latent)

        probs  = F.softmax(logits, dim=-1).squeeze(0)
        action = int(probs.argmax().item())
        conf   = float(probs[action].item())
        exp_r  = float(val.item())

        return action, conf, exp_r, enc_out.squeeze(0).tolist()

    def count_parameters(self) -> int:
        return sum(p.numel() for p in self.parameters() if p.requires_grad)


# ── Action labels ─────────────────────────────────────────────────────────────

ACTION_LABELS = {0: "HOLD", 1: "BUY", 2: "SELL", 3: "CLOSE"}


if __name__ == "__main__":
    # Quick sanity check — no GPU needed
    agent = ApexRLAgent(n_features=12, n_actions=4)
    print(f"Agent parameters: {agent.count_parameters():,}")

    dummy_obs = np.random.randn(50, 12).astype(np.float32)
    action, conf, exp_r = agent.predict(dummy_obs)
    print(f"Action: {ACTION_LABELS[action]}  Confidence: {conf:.3f}  Expected R: {exp_r:.3f}")

    # Batch forward pass
    batch = torch.randn(32, 50, 12)
    logits, values = agent.act(batch)
    print(f"Logits shape: {logits.shape}  Values shape: {values.shape}")
    print("Architecture OK")
