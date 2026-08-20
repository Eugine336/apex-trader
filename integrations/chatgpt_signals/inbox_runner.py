from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .binance_paper_executor import BinancePaperExecutor
from .schema import ChatGPTTradeSignal

REPO = os.getenv("APEX_REPO", "Eugine336/apex-trader")
SIGNAL_BRANCH = os.getenv("CHATGPT_SIGNAL_BRANCH", "chatgpt-signal-inbox")
POLL_SECONDS = max(2, float(os.getenv("CHATGPT_SIGNAL_POLL_SECONDS", "3")))
WORKTREE = Path(os.getenv("CHATGPT_SIGNAL_WORKTREE", ".runtime/chatgpt-signal-inbox"))


def run_git(*args: str) -> str:
    p = subprocess.run(["git", *args], cwd=Path.cwd(), text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "git command failed")
    return p.stdout


def ensure_branch_checkout() -> None:
    WORKTREE.parent.mkdir(parents=True, exist_ok=True)
    if not (WORKTREE / ".git").exists():
        subprocess.run(
            ["git", "clone", "--branch", SIGNAL_BRANCH,
             f"https://github.com/{REPO}.git", str(WORKTREE)],
            check=True,
        )
    else:
        subprocess.run(["git", "fetch", "origin", SIGNAL_BRANCH], cwd=WORKTREE, check=True)
        subprocess.run(["git", "reset", "--hard", f"origin/{SIGNAL_BRANCH}"], cwd=WORKTREE, check=True)


def consume_files(executor: BinancePaperExecutor) -> None:
    inbox = WORKTREE / "signals" / "inbox"
    if not inbox.exists():
        return
    for path in sorted(inbox.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            signal = ChatGPTTradeSignal.from_dict(raw)
            fill = executor.execute(signal)
            print(
                f"CHATGPT SIGNAL ACCEPTED: {fill.signal_id} "
                f"{fill.side} {fill.quantity} {fill.symbol} @ {fill.fill_price}"
            )
            # Rename locally so a transient branch reset cannot re-run the same file.
            path.rename(path.with_suffix(".consumed.json"))
        except Exception as exc:
            print(f"CHATGPT SIGNAL REJECTED: {path.name}: {exc}")


def main() -> None:
    print("APEX ChatGPT signal executor — PAPER MODE ONLY")
    print(f"signal branch={SIGNAL_BRANCH}, poll={POLL_SECONDS}s")
    executor = BinancePaperExecutor()
    while True:
        try:
            ensure_branch_checkout()
            consume_files(executor)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"signal transport unavailable: {exc}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
