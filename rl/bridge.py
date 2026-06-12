"""
APEX RL — Scanner Bridge
=========================
Connects the RL subsystem to the existing APEX scanner
without modifying any existing scanner code.

Drop-in integration pattern:
  1. Import RLBridge in your scanner/pair_scanner.py
  2. Call bridge.augment_score(pair, base_score, obs) per pair
  3. The bridge handles authority checks, signal generation,
     shadow journaling, and score modification internally.

The existing scanner remains the primary authority at all stages.
The RL contribution grows only as authority is earned.
"""

from __future__ import annotations

import numpy as np
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .shadow import ShadowEngine, RLSignal
from .authority import AuthorityManager, Permissions

logger = logging.getLogger("apex.rl.bridge")


# ── Augmented score result ────────────────────────────────────────────────────

@dataclass
class AugmentedScore:
    pair:           str
    base_score:     float       # original APEX scanner score
    rl_delta:       float       # how much RL added or subtracted
    final_score:    float       # base + rl_delta
    rl_action:      int         # 0=HOLD 1=BUY 2=SELL 3=CLOSE
    rl_confidence:  float       # agent confidence
    rl_expected_r:  float       # agent's R estimate
    vetoed:         bool        # True if RL vetoed the trade
    authority_stage: int
    authority_label: str


# ── Bridge ────────────────────────────────────────────────────────────────────

class RLBridge:
    """
    The single integration point between the RL subsystem and APEX.

    Usage in pair_scanner.py:

        # At module init
        self.rl_bridge = RLBridge(
            checkpoint="checkpoints/apex_rl_best.pt",
            shadow_db="shadow_journal.db",
            authority_db="authority.db",
        )

        # In scan loop per pair
        result = self.rl_bridge.augment_score(
            pair="EURUSD",
            base_score=76.5,
            obs=obs_array,        # shape (50, 12) numpy float32
            close=1.0823,
            atr=0.00045,
            pip_size=0.0001,
        )

        if result.vetoed:
            continue  # skip this pair

        final_score = result.final_score
    """

    # Minimum confidence for veto to trigger
    VETO_CONFIDENCE_THRESHOLD = 0.70
    # Minimum confidence before RL signal counts for anything
    MIN_SIGNAL_CONFIDENCE     = 0.55

    def __init__(
        self,
        checkpoint:   str,
        shadow_db:    str = "shadow_journal.db",
        authority_db: str = "authority.db",
        enabled:      bool = True,
    ):
        self.enabled   = enabled
        self.authority = AuthorityManager(authority_db)
        self._shadow_store = None
        self._live_trade_count = 0

        if enabled:
            try:
                self.shadow = ShadowEngine(checkpoint, shadow_db)
                logger.info(f"[RLBridge] Loaded. Stage: {self.authority.stage_label}")
                try:
                    from persistence.shadow_store import ShadowStore
                    self._shadow_store = ShadowStore()
                except Exception:
                    logger.debug("[RLBridge] ShadowStore unavailable; RL shadow contracts will not be persisted to Phase 4 store")
            except Exception as e:
                logger.warning(f"[RLBridge] Failed to load checkpoint: {e}. Disabling.")
                self.enabled = False
                self.shadow  = None
        else:
            self.shadow = None

    # ── Main integration point ────────────────────────────────────────────

    def augment_score(
        self,
        pair:       str,
        base_score: float,
        obs:        np.ndarray,
        close:      float   = 0.0,
        atr:        float   = 0.0,
        pip_size:   float   = 0.0001,
        context_vec: Optional[np.ndarray] = None,
        symbol_id:  Optional[int] = None,
        base_direction: Optional[int] = None,
    ) -> AugmentedScore:
        """
        Primary method called per pair per scan cycle.
        Returns augmented score with RL influence applied
        according to current authority stage.
        """

        # Passthrough if disabled or Stage 1 (journal only)
        if not self.enabled or self.authority.stage < 2:
            return AugmentedScore(
                pair=pair, base_score=base_score, rl_delta=0.0,
                final_score=base_score, rl_action=0, rl_confidence=0.0,
                rl_expected_r=0.0, vetoed=False,
                authority_stage=self.authority.stage,
                authority_label=self.authority.stage_label,
            )

        # Get RL signal
        try:
            signal = self.shadow.get_signal(
                pair, obs, context_vec=context_vec, symbol_id=symbol_id,
            )
        except Exception as e:
            logger.error(f"[RLBridge] Signal error for {pair}: {e}")
            return self._passthrough(pair, base_score)

        perms    = self.authority.get_permissions()
        rl_delta = 0.0
        vetoed   = False

        # ── Stage 2: shadow compare only, no score influence ─────────────
        if perms.stage == 2:
            self._handle_shadow(pair, signal, close, atr, pip_size)
            rl_delta = 0.0

        # ── Stage 3+: confidence signal contributes to score ─────────────
        elif perms.stage >= 3 and signal.confidence >= self.MIN_SIGNAL_CONFIDENCE:
            resolved_dir = base_direction if base_direction is not None else (1 if base_score >= 60 else -1)
            rl_direction   = 1 if signal.action == 1 else (-1 if signal.action == 2 else 0)

            alignment = resolved_dir * rl_direction  # +1 agree, -1 disagree, 0 neutral

            rl_delta = (
                alignment
                * signal.expected_r
                * signal.confidence
                * perms.rank_weight
                * 10.0   # scale to score units
            )
            rl_delta = float(np.clip(rl_delta, -15.0, 15.0))

            self._handle_shadow(pair, signal, close, atr, pip_size)

        # ── Stage 5+: veto authority ──────────────────────────────────────
        if perms.can_veto and signal.confidence >= self.VETO_CONFIDENCE_THRESHOLD:
            resolved_dir_veto = base_direction if base_direction is not None else (1 if base_score >= 60 else -1)
            rl_direction   = 1 if signal.action == 1 else (-1 if signal.action == 2 else 0)

            # Veto only when RL strongly disagrees with scanner direction
            if rl_direction != 0 and rl_direction != resolved_dir_veto:
                vetoed = True
                logger.info(
                    f"[RLBridge] VETO {pair} | scanner={'BUY' if resolved_dir_veto==1 else 'SELL'} "
                    f"rl={signal.action_label} conf={signal.confidence:.2f}"
                )

        final_score = float(np.clip(base_score + rl_delta, 0.0, 100.0))

        return AugmentedScore(
            pair=pair,
            base_score=base_score,
            rl_delta=round(rl_delta, 3),
            final_score=round(final_score, 3),
            rl_action=signal.action,
            rl_confidence=round(signal.confidence, 4),
            rl_expected_r=round(signal.expected_r, 4),
            vetoed=vetoed,
            authority_stage=perms.stage,
            authority_label=perms.label,
        )

    def update_price(
        self,
        pair:     str,
        high:     float,
        low:      float,
        close:    float,
        atr:      float,
        obs:      np.ndarray,
    ):
        """Call every bar to update shadow trade management."""
        if self.enabled and self.shadow:
            self.shadow.update_price(pair, high, low, close, atr, obs)

    def evaluate_authority(self) -> dict:
        """
        Run authority promotion/demotion check.
        Call this periodically (e.g. daily or after N trades).
        Returns the decision dict from AuthorityManager.
        """
        if not self.enabled or not self.shadow:
            return {"action": "DISABLED"}

        metrics = self.shadow.shadow_score()
        metrics["n_live_trades"] = self._live_trade_count
        # P11: the bridge only stays enabled when a checkpoint loaded, so its
        # presence is the gate the authority needs for the Stage 1→2 promotion.
        metrics["checkpoint_loaded"] = True

        result = self.authority.evaluate(metrics)
        logger.info(f"[RLBridge] Authority eval: {result}")
        return result

    def status(self) -> dict:
        """Current status summary for dashboard/logging."""
        perms = self.authority.get_permissions()
        score = self.shadow.shadow_score() if self.shadow else {}
        return {
            "enabled":        self.enabled,
            "stage":          perms.stage,
            "label":          perms.label,
            "can_veto":       perms.can_veto,
            "can_rank":       perms.can_rank,
            "has_trade_auth": perms.has_trade_auth,
            "max_pos_pct":    perms.max_position_pct,
            "shadow_score":   score,
        }

    def record_live_trade(self):
        """Increment the live trade counter (resets on restart)."""
        self._live_trade_count += 1

    # ── Internal ─────────────────────────────────────────────────────────

    def _handle_shadow(
        self,
        pair:     str,
        signal:   RLSignal,
        close:    float,
        atr:      float,
        pip_size: float,
    ):
        if signal.action in (1, 2) and signal.confidence >= self.MIN_SIGNAL_CONFIDENCE:
            self.shadow.open_shadow_trade(pair, signal, close, atr, pip_size)
            self._persist_rl_shadow_contract(pair, signal, close, atr, pip_size)

    def _passthrough(self, pair: str, base_score: float) -> AugmentedScore:
        return AugmentedScore(
            pair=pair, base_score=base_score, rl_delta=0.0,
            final_score=base_score, rl_action=0, rl_confidence=0.0,
            rl_expected_r=0.0, vetoed=False,
            authority_stage=self.authority.stage,
            authority_label=self.authority.stage_label,
        )

    def _persist_rl_shadow_contract(
        self,
        pair: str,
        signal: RLSignal,
        close: float,
        atr: float,
        pip_size: float,
    ):
        if self._shadow_store is None:
            return
        try:
            from persistence.shadow_store import ShadowContract, new_contract_id

            direction_int = 1 if signal.action == 1 else -1
            spread = 1.5 * pip_size
            entry = close + (spread if direction_int == 1 else -spread)
            sl = entry - direction_int * 1.5 * atr
            tp1 = entry + direction_int * 2.0 * atr
            tp2 = entry + direction_int * 3.0 * atr

            contract = ShadowContract(
                contract_id=new_contract_id(),
                symbol=pair,
                direction="LONG" if direction_int == 1 else "SHORT",
                entry_price=entry,
                stop_loss=sl,
                tp1=tp1,
                tp2=tp2,
                pip_size=pip_size,
                rejecting_gate="rl_shadow",
                ts_utc_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
                score=0,
            )
            self._shadow_store.insert_contract(contract)
        except Exception as exc:
            logger.debug("[RLBridge] Phase 4 shadow contract persistence failed: %s", exc)
