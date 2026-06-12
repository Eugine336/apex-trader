"""
APEX RL — Vectorized Environment
=================================
Run N copies of ``ApexMultiTFTradingEnv`` in parallel to keep the GPU
fed during PPO rollout collection.

Single-thread rollout collection bottlenecks training at ~8-9 steps/sec
on a T4: one Python process steps the environment sequentially while the
GPU sits idle waiting for the next batch of transitions. Running N
environments in separate processes multiplies collection throughput
roughly N× and turns a multi-day run into an overnight one.

Two implementations share one interface:

* ``DummyVecEnv``   — in-process, sequential. Zero IPC overhead; used as
  the ``n_envs == 1`` fallback and for debugging.
* ``SubprocVecEnv`` — one worker process per environment, point-to-point
  ``Pipe`` for low-latency action/observation exchange. Workers persist
  across curriculum stages and rebuild their env on ``reload()`` so the
  process pool is created exactly once per training run.

Both auto-reset an environment when it returns ``done`` and return the
first observation of the next episode, matching the rollout convention
used by ``MTFPPOTrainer``.

The workers never import torch or touch CUDA — they only step the pure
numpy/pandas environment — so the choice of start method ("fork" vs
"spawn") is safe even when the parent process has already initialised a
CUDA context for the agent.
"""

from __future__ import annotations

import multiprocessing as mp
from typing import Optional

import numpy as np
from loguru import logger

from .contracts import TF_ORDER


# ── Environment factory ─────────────────────────────────────────────────────


def make_mtf_env(env_kwargs: dict):
    """Construct a single ``ApexMultiTFTradingEnv`` from picklable kwargs.

    Imported lazily so worker processes (under "spawn"/"forkserver") build
    the environment fresh without dragging torch into the worker.
    """
    from .mtf_environment import ApexMultiTFTradingEnv

    return ApexMultiTFTradingEnv(**env_kwargs)


def _default_start_method() -> str:
    """Prefer ``fork`` (fast, low overhead) when available, else ``spawn``.

    Workers are torch-free and never use CUDA, so forking after the parent
    has initialised CUDA is safe here.
    """
    methods = mp.get_all_start_methods()
    if "fork" in methods:
        return "fork"
    return "spawn"


def _stack_obs(results: list[tuple]):
    """Stack a list of ``(obs, ctx, sym)`` tuples into batched arrays."""
    obs = np.stack([r[0] for r in results]).astype(np.float32)
    ctx = np.stack([r[1] for r in results]).astype(np.float32)
    sym = np.asarray([r[2] for r in results], dtype=np.int64)
    return obs, ctx, sym


# ── In-process fallback ──────────────────────────────────────────────────────


class DummyVecEnv:
    """Sequential, in-process vector env. Used when ``n_envs == 1``.

    Behaviour matches ``SubprocVecEnv`` exactly (auto-reset on done) but
    without any inter-process communication, so the single-environment path
    stays equivalent to the original trainer.
    """

    def __init__(self, env_kwargs_list: list[dict]):
        self.envs = [make_mtf_env(k) for k in env_kwargs_list]
        self.num_envs = len(self.envs)

    def reset(self):
        return _stack_obs([e.reset() for e in self.envs])

    def step(self, actions):
        obs_l, ctx_l, sym_l, rew_l, done_l, info_l = [], [], [], [], [], []
        for env, a in zip(self.envs, actions):
            (obs, ctx, sym), reward, done, info = env.step(int(a))
            if done:
                obs, ctx, sym = env.reset()
            obs_l.append(obs)
            ctx_l.append(ctx)
            sym_l.append(sym)
            rew_l.append(reward)
            done_l.append(done)
            info_l.append(info)
        return (
            np.stack(obs_l).astype(np.float32),
            np.stack(ctx_l).astype(np.float32),
            np.asarray(sym_l, dtype=np.int64),
            np.asarray(rew_l, dtype=np.float32),
            np.asarray(done_l, dtype=np.float32),
            info_l,
        )

    def reload(self, env_kwargs_list: list[dict]):
        """Rebuild every environment (used between curriculum instruments)."""
        self.envs = [make_mtf_env(k) for k in env_kwargs_list]
        self.num_envs = len(self.envs)
        return self.reset()

    def close(self):
        self.envs = []


# ── Subprocess worker ────────────────────────────────────────────────────────


def _worker(remote, env_kwargs: dict):
    """Run one environment, serving commands over the pipe."""
    env = make_mtf_env(env_kwargs)
    try:
        while True:
            cmd, data = remote.recv()
            if cmd == "step":
                (obs, ctx, sym), reward, done, info = env.step(int(data))
                if done:
                    # Auto-reset so the next rollout step acts on a fresh
                    # episode; the done flag still marks the boundary for GAE.
                    obs, ctx, sym = env.reset()
                remote.send(((obs, ctx, sym), float(reward), bool(done), info))
            elif cmd == "reset":
                remote.send(env.reset())
            elif cmd == "reload":
                env = make_mtf_env(data)
                remote.send(env.reset())
            elif cmd == "close":
                remote.send(True)
                break
            else:
                raise RuntimeError(f"[vec_env worker] unknown command: {cmd}")
    except (KeyboardInterrupt, EOFError):
        pass
    except Exception as exc:  # pragma: no cover - surfaced to parent for restart
        try:
            remote.send(("__error__", repr(exc)))
        except Exception:
            pass
    finally:
        try:
            remote.close()
        except Exception:
            pass


_ERROR_TAG = "__error__"


class SubprocVecEnv:
    """One worker process per environment, communicating over ``Pipe``.

    The worker pool is created once and reused across curriculum stages via
    :meth:`reload`. Dead workers are detected on ``recv`` and transparently
    restarted so a single environment crash never kills the whole run.
    """

    def __init__(self, env_kwargs_list: list[dict], start_method: Optional[str] = None):
        self.num_envs = len(env_kwargs_list)
        self._kwargs = list(env_kwargs_list)
        self._ctx = mp.get_context(start_method or _default_start_method())
        self.remotes: list = []
        self.processes: list = []
        for k in env_kwargs_list:
            self.remotes.append(None)
            self.processes.append(None)
        for i in range(self.num_envs):
            self._spawn(i)
        self.closed = False

    # ── Process lifecycle ────────────────────────────────────────────────

    def _spawn(self, i: int):
        parent, child = self._ctx.Pipe()
        proc = self._ctx.Process(target=_worker, args=(child, self._kwargs[i]), daemon=True)
        proc.start()
        child.close()
        self.remotes[i] = parent
        self.processes[i] = proc

    def _restart(self, i: int):
        logger.warning("[vec_env] restarting worker {}", i)
        try:
            proc = self.processes[i]
            if proc is not None:
                proc.terminate()
                proc.join(timeout=5)
        except Exception:
            pass
        try:
            if self.remotes[i] is not None:
                self.remotes[i].close()
        except Exception:
            pass
        self._spawn(i)

    # ── Receive helpers (with crash recovery) ────────────────────────────

    def _recv_obs(self, i: int):
        """Receive a ``(obs, ctx, sym)`` triple, restarting on failure."""
        try:
            msg = self.remotes[i].recv()
        except (EOFError, OSError):
            return self._restart_obs(i)
        if isinstance(msg, tuple) and len(msg) == 2 and msg[0] == _ERROR_TAG:
            logger.error("[vec_env] worker {} error: {}", i, msg[1])
            return self._restart_obs(i)
        return msg

    def _restart_obs(self, i: int):
        self._restart(i)
        self.remotes[i].send(("reset", None))
        return self.remotes[i].recv()

    def _recv_step(self, i: int):
        """Receive a step result, restarting (as a done boundary) on failure."""
        try:
            msg = self.remotes[i].recv()
        except (EOFError, OSError):
            return self._restart_step(i)
        if isinstance(msg, tuple) and len(msg) == 2 and msg[0] == _ERROR_TAG:
            logger.error("[vec_env] worker {} error: {}", i, msg[1])
            return self._restart_step(i)
        return msg

    def _restart_step(self, i: int):
        self._restart(i)
        self.remotes[i].send(("reset", None))
        obs, ctx, sym = self.remotes[i].recv()
        return ((obs, ctx, sym), 0.0, True, {"restarted": True})

    # ── Public API ───────────────────────────────────────────────────────

    def reset(self):
        for r in self.remotes:
            r.send(("reset", None))
        return _stack_obs([self._recv_obs(i) for i in range(self.num_envs)])

    def step(self, actions):
        for r, a in zip(self.remotes, actions):
            r.send(("step", int(a)))
        obs_l, ctx_l, sym_l, rew_l, done_l, info_l = [], [], [], [], [], []
        for i in range(self.num_envs):
            (obs, ctx, sym), reward, done, info = self._recv_step(i)
            obs_l.append(obs)
            ctx_l.append(ctx)
            sym_l.append(sym)
            rew_l.append(reward)
            done_l.append(done)
            info_l.append(info)
        return (
            np.stack(obs_l).astype(np.float32),
            np.stack(ctx_l).astype(np.float32),
            np.asarray(sym_l, dtype=np.int64),
            np.asarray(rew_l, dtype=np.float32),
            np.asarray(done_l, dtype=np.float32),
            info_l,
        )

    def reload(self, env_kwargs_list: list[dict]):
        """Rebuild each worker's environment in place (curriculum switch)."""
        if len(env_kwargs_list) != self.num_envs:
            raise ValueError(
                f"reload expects {self.num_envs} kwargs, got {len(env_kwargs_list)}"
            )
        self._kwargs = list(env_kwargs_list)
        for r, k in zip(self.remotes, env_kwargs_list):
            r.send(("reload", k))
        return _stack_obs([self._recv_obs(i) for i in range(self.num_envs)])

    def close(self):
        if getattr(self, "closed", True):
            return
        for r in self.remotes:
            try:
                r.send(("close", None))
            except Exception:
                pass
        for p in self.processes:
            try:
                p.join(timeout=5)
                if p.is_alive():
                    p.terminate()
            except Exception:
                pass
        for r in self.remotes:
            try:
                r.close()
            except Exception:
                pass
        self.closed = True

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# ── Factory ──────────────────────────────────────────────────────────────────


def make_vec_env(
    env_kwargs_list: list[dict],
    start_method: Optional[str] = None,
):
    """Return a ``DummyVecEnv`` for a single env, else a ``SubprocVecEnv``."""
    if len(env_kwargs_list) <= 1:
        return DummyVecEnv(env_kwargs_list)
    return SubprocVecEnv(env_kwargs_list, start_method=start_method)


def has_instrument_data(data_dir: str, instrument: str) -> bool:
    """True if all required timeframe CSVs exist for *instrument*."""
    from pathlib import Path

    d = Path(data_dir)
    return all((d / f"{instrument.upper()}_{tf}.csv").exists() for tf in TF_ORDER)
