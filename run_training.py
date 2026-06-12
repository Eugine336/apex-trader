"""
APEX RL — Training Entry Point
================================
One command to train the multi-timeframe (48-feature, symbol-conditioned)
generalist agent across every instrument that has complete historical data.

What it does:
  1. Auto-discovers symbols in the data dir with all 4 required timeframes
     (M5, M15, H1, H4) and validates each against the INSTRUMENT_REGISTRY.
  2. Runs curriculum training (round-robin across all valid symbols) using
     ``MTFPPOTrainer``. A single ``--instrument`` may be trained instead.
  3. Copies the best checkpoint (``apex_rl_mtf_best.pt``) to the filename the
     live system loads (``apex_rl_best.pt``) so no manual rename is needed.
  4. Runs the walk-forward evaluator on a few key symbols as a sanity check.
  5. Prints a summary of results.

Usage::

    python run_training.py                      # full curriculum, 5M steps
    python run_training.py --smoke              # quick 200k-step validation
    python run_training.py --steps 2000000      # custom step budget
    python run_training.py --instrument EURUSD  # single-symbol specialist
    python run_training.py --data-dir data --gpu

The produced ``.pt`` files live in ``checkpoints/`` and are gitignored.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

# Allow running as a plain script from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Timeframes the MTF environment requires for every instrument.
REQUIRED_TIMEFRAMES = ["M5", "M15", "H1", "H4"]

# Symbols evaluated as a post-training sanity check (only those actually trained
# are used).
DEFAULT_EVAL_SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD", "US100"]

FULL_STEPS = 5_000_000
SMOKE_STEPS = 200_000

MTF_BEST_NAME = "apex_rl_mtf_best.pt"
LIVE_NAME = "apex_rl_best.pt"


def _require_torch():
    """Import torch lazily so missing deps produce a clear message."""
    try:
        import torch  # noqa: F401
    except ImportError:
        print(
            "ERROR: PyTorch is not installed. RL training requires torch.\n"
            "  Install (CPU):  pip install torch\n"
            "  Install (CUDA): see https://pytorch.org/get-started/locally/\n"
        )
        sys.exit(1)


def discover_trainable_symbols(data_dir: str) -> list[str]:
    """
    Return sorted symbols that (a) have all required timeframe CSVs in
    *data_dir* and (b) are registered as non-synthetic instruments.
    """
    d = Path(data_dir)
    if not d.is_dir():
        print(f"ERROR: data directory not found: {d.resolve()}")
        sys.exit(1)

    try:
        from config import INSTRUMENT_REGISTRY, InstrumentCategory
    except ImportError as exc:
        print(f"ERROR: could not import INSTRUMENT_REGISTRY from config: {exc}")
        sys.exit(1)

    valid: list[str] = []
    skipped_incomplete: list[str] = []
    skipped_synthetic: list[str] = []

    for symbol, info in INSTRUMENT_REGISTRY.items():
        if info.category == InstrumentCategory.SYNTHETIC:
            skipped_synthetic.append(symbol)
            continue
        has_all = all((d / f"{symbol}_{tf}.csv").exists() for tf in REQUIRED_TIMEFRAMES)
        if has_all:
            valid.append(symbol)
        else:
            skipped_incomplete.append(symbol)

    valid.sort()

    print(f"[discover] data dir: {d.resolve()}")
    print(f"[discover] trainable symbols ({len(valid)}): {', '.join(valid) or '(none)'}")
    if skipped_incomplete:
        print(
            f"[discover] skipped (incomplete data, {len(skipped_incomplete)}): "
            f"{', '.join(sorted(skipped_incomplete))}"
        )
    if skipped_synthetic:
        print(
            f"[discover] skipped (synthetic, no historical data, "
            f"{len(skipped_synthetic)}): {', '.join(sorted(skipped_synthetic))}"
        )

    return valid


def promote_checkpoint(save_dir: str) -> Path | None:
    """
    Copy the best MTF checkpoint to the filename the live system loads.

    Returns the destination path on success, ``None`` if no source exists.
    """
    src = Path(save_dir) / MTF_BEST_NAME
    if not src.exists():
        print(
            f"[promote] WARNING: {src} not found — no 'best' checkpoint was "
            f"saved (training may have been too short to record one)."
        )
        return None
    dst = Path(save_dir) / LIVE_NAME
    shutil.copyfile(src, dst)
    print(f"[promote] copied {src.name} -> {dst.name} (live system will load this)")
    return dst


def run_evaluation(checkpoint: Path, data_dir: str, symbols: list[str]) -> None:
    """Run walk-forward evaluation on *symbols* and print a compact summary."""
    from rl.evaluator import evaluate_checkpoint

    print("\n" + "=" * 70)
    print("EVALUATION (walk-forward, out-of-sample)")
    print("=" * 70)

    for sym in symbols:
        try:
            report = evaluate_checkpoint(str(checkpoint), data_dir, sym, n_folds=5)
        except Exception as exc:
            print(f"  {sym:<8} — evaluation skipped: {exc}")
            continue
        verdict = "PASS" if report.consistent else "WEAK"
        print(
            f"  {sym:<8} [{verdict}] trades={report.total_trades:<5} "
            f"WR={report.mean_win_rate:.1%} "
            f"exp={report.mean_expectancy:+.3f}R "
            f"dd={report.mean_max_drawdown:.1%} "
            f"sharpe={report.mean_sharpe:+.2f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train the APEX multi-timeframe RL agent.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=FULL_STEPS,
        help="Total training steps (across all curriculum symbols).",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help=f"Quick validation run ({SMOKE_STEPS:,} steps). Overrides --steps.",
    )
    parser.add_argument(
        "--instrument",
        type=str,
        default=None,
        help="Train a single-symbol specialist instead of the full curriculum.",
    )
    parser.add_argument("--data-dir", type=str, default="data", help="Directory with the CSV data.")
    parser.add_argument(
        "--n-envs",
        type=int,
        default=8,
        help="Parallel environments for rollout collection (1 = single-process "
             "fallback). Higher values keep the GPU fed and scale throughput ~N×.",
    )
    parser.add_argument(
        "--gpu",
        action="store_true",
        help="Require a CUDA GPU; warn (do not abort) if unavailable.",
    )
    parser.add_argument(
        "--no-eval",
        action="store_true",
        help="Skip the post-training walk-forward evaluation.",
    )
    args = parser.parse_args()

    _require_torch()
    import torch

    from rl.mtf_trainer import MTFPPOConfig, MTFPPOTrainer

    cuda = torch.cuda.is_available()
    if args.gpu and not cuda:
        print(
            "[gpu] WARNING: --gpu requested but no CUDA device is available. "
            "Training will run on CPU and may be very slow."
        )
    print(f"[device] {'CUDA' if cuda else 'CPU'} "
          f"({torch.cuda.get_device_name(0) if cuda else 'no GPU'})")

    total_steps = SMOKE_STEPS if args.smoke else args.steps
    if total_steps <= 0:
        print("ERROR: --steps must be positive.")
        sys.exit(1)

    n_envs = max(1, args.n_envs)
    if n_envs != args.n_envs:
        print(f"[train] n_envs floored to {n_envs} (must be >= 1)")

    reward_shaping = {
        "hold_penalty": -0.001,
        "quick_loss_penalty": -0.5,
        "timeout_penalty": -0.3,
    }

    # ── Resolve the training set ──────────────────────────────────────────
    if args.instrument:
        symbol = args.instrument.upper()
        missing = [
            tf for tf in REQUIRED_TIMEFRAMES
            if not (Path(args.data_dir) / f"{symbol}_{tf}.csv").exists()
        ]
        if missing:
            print(
                f"ERROR: {symbol} is missing timeframe data: {', '.join(missing)} "
                f"in {Path(args.data_dir).resolve()}"
            )
            sys.exit(1)
        train_symbols = [symbol]
    else:
        train_symbols = discover_trainable_symbols(args.data_dir)
        if not train_symbols:
            print("ERROR: no trainable symbols found. Nothing to do.")
            sys.exit(1)

    start_instrument = train_symbols[0]
    print(
        f"\n[train] mode={'single' if args.instrument else 'curriculum'} | "
        f"symbols={len(train_symbols)} | total_steps={total_steps:,} | "
        f"n_envs={n_envs} | smoke={args.smoke}"
    )

    cfg = MTFPPOConfig(
        data_dir=args.data_dir,
        instrument=start_instrument,
        total_steps=total_steps,
        n_envs=n_envs,
        save_dir="checkpoints",
        log_path="training_log_mtf.json",
        reward_shaping=reward_shaping,
    )
    trainer = MTFPPOTrainer(cfg)

    t0 = time.time()
    if args.instrument:
        trainer.train()
    else:
        trainer.train_curriculum(train_symbols)
    elapsed = time.time() - t0
    print(f"\n[train] complete in {elapsed / 60:.1f} min")

    # ── Promote the best checkpoint to the live filename ──────────────────
    live_ckpt = promote_checkpoint(cfg.save_dir)

    # ── Post-training sanity evaluation ───────────────────────────────────
    if live_ckpt and not args.no_eval:
        eval_symbols = [s for s in DEFAULT_EVAL_SYMBOLS if s in train_symbols]
        if not eval_symbols:
            eval_symbols = train_symbols[: min(3, len(train_symbols))]
        run_evaluation(live_ckpt, args.data_dir, eval_symbols)

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  symbols trained : {len(train_symbols)}")
    print(f"  total steps     : {total_steps:,}")
    print(f"  best reward     : {trainer.best_reward:.4f}")
    print(f"  training time   : {elapsed / 60:.1f} min")
    if live_ckpt:
        print(f"  live checkpoint : {live_ckpt}")
        print("  -> restart the trading loop; the scanner will load this checkpoint.")
    else:
        print("  live checkpoint : NOT created (no best checkpoint saved)")
    print("=" * 70)


if __name__ == "__main__":
    main()
