"""APEX TRADER — Modal.com serverless-GPU inference for the LLM council.

This script deploys two OpenAI-compatible inference endpoints on Modal's GPUs,
one per model, so APEX's reasoning subsystem can reach them exactly like any
other vLLM server (see ``llm/client.py`` — an unknown provider with a
``base_url`` is treated as OpenAI-compatible):

    * ``modal-mistral``  → ``mistralai/Mistral-7B-Instruct-v0.3``
    * ``modal-qwen``     → ``Qwen/Qwen2.5-7B-Instruct``

Why Modal: the VPS has ~1.8 GB RAM and no GPU, so **all** inference must run off
the box. Modal spins an A10G (24 GB VRAM — ample for a 7B model) only while a
request is in flight, keeps it warm for ``SCALEDOWN_WINDOW`` seconds, then
scales to zero so GPU billing stops. The VPS only ever makes HTTP calls.

Each model is served by vLLM's built-in OpenAI-compatible server, launched via
its **stable CLI** (``python -m vllm.entrypoints.openai.api_server``) behind a
Modal ``@modal.web_server``. The CLI is stable across vLLM releases, unlike the
Python internals (``build_app``, ``serving_engine`` …) which move every version.
It implements ``/v1/chat/completions``, ``/v1/completions`` and ``/v1/models``
automatically — no request shaping is written here.

──────────────────────────────────────────────────────────────────────────────
DEPLOY
──────────────────────────────────────────────────────────────────────────────
    pip install modal
    modal token new                       # one-time browser auth
    modal deploy infra/modal_vllm_deploy.py

Modal prints the public endpoint URLs on deploy, e.g.

    https://<your-workspace>--apex-trader-llm-mistral-serve.modal.run
    https://<your-workspace>--apex-trader-llm-qwen-serve.modal.run

Append ``/v1`` to each and paste them into ``LLM_EXTRA_MODELS`` in ``.env``
(replace ``YOUR_USERNAME`` with your Modal workspace name).

OPTIONAL BEARER-TOKEN AUTH
    modal secret create apex-inference-key MODAL_INFERENCE_KEY=<random-token>
Then add ``secrets=[modal.Secret.from_name("apex-inference-key")]`` to each
``@app.cls()`` decorator so ``MODAL_INFERENCE_KEY`` is present in the container;
vLLM's own ``--api-key`` flag (wired below from that env var) enforces
``Authorization: Bearer <token>`` on every request. Set the same value as
``MODAL_INFERENCE_KEY`` in ``.env`` so APEX sends it. Leave it unset to run open
(the endpoints are still obscured by an unguessable Modal URL).
"""

from __future__ import annotations

import os

import modal

# ── App + image ───────────────────────────────────────────────────────────────
app = modal.App("apex-trader-llm")

# vLLM (+ its OpenAI server deps) baked into the image so cold starts skip pip.
vllm_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("vllm>=0.5.0", "fastapi", "uvicorn")
)

# Persistent HuggingFace cache so a model is downloaded once, not per cold start.
model_cache = modal.Volume.from_name("apex-model-cache", create_if_missing=True)
CACHE_DIR = "/root/.cache/huggingface"

# GPU billing stops once a container is idle this long (5 min warm window).
SCALEDOWN_WINDOW = 300

# Optional bearer-token auth (off by default). To arm it later:
#   1. Create a Modal secret holding the token, e.g.
#        modal secret create apex-inference-key MODAL_INFERENCE_KEY=<token>
#   2. Add ``secrets=[modal.Secret.from_name("apex-inference-key")]`` to each
#      ``@app.cls()`` decorator below so MODAL_INFERENCE_KEY is present in the
#      container environment.
#   3. Set the same value as MODAL_INFERENCE_KEY in APEX's .env.
# Each server reads MODAL_INFERENCE_KEY from the environment and, when set,
# passes it to vLLM's ``--api-key`` flag (built-in bearer-token enforcement). It
# is NOT wired by default: ``Secret.from_name`` is validated at deploy time and
# would abort the deploy with "Secret not found" if the secret does not exist.

VLLM_PORT = 8000
# Cold-start budget: time for vLLM to import, download (first run) and load the
# model weights onto the GPU before Modal probes the port.
STARTUP_TIMEOUT = 300


def _vllm_command(model_name: str) -> list:
    """Build the stable-CLI command that serves ``model_name`` over OpenAI's API.

    Uses ``python -m vllm.entrypoints.openai.api_server`` — vLLM's supported CLI
    entrypoint, stable across releases (unlike the Python internals). When
    ``MODAL_INFERENCE_KEY`` is present it is passed to vLLM's ``--api-key`` for
    built-in bearer-token auth; absent, the server runs open.
    """
    cmd = [
        "python", "-m", "vllm.entrypoints.openai.api_server",
        "--model", model_name,
        "--host", "0.0.0.0",
        "--port", str(VLLM_PORT),
        "--gpu-memory-utilization", "0.90",
    ]
    env_key = os.environ.get("MODAL_INFERENCE_KEY", "").strip()
    if env_key:
        cmd += ["--api-key", env_key]
    return cmd


# ── Mistral 7B endpoint ───────────────────────────────────────────────────────
@app.cls(
    image=vllm_image,
    gpu="a10g",
    volumes={CACHE_DIR: model_cache},
    scaledown_window=SCALEDOWN_WINDOW,
)
class MistralServer:
    MODEL = "mistralai/Mistral-7B-Instruct-v0.3"

    @modal.web_server(port=VLLM_PORT, startup_timeout=STARTUP_TIMEOUT)
    def serve(self):
        import subprocess
        subprocess.Popen(_vllm_command(self.MODEL))


# ── Qwen 7B endpoint ──────────────────────────────────────────────────────────
@app.cls(
    image=vllm_image,
    gpu="a10g",
    volumes={CACHE_DIR: model_cache},
    scaledown_window=SCALEDOWN_WINDOW,
)
class QwenServer:
    MODEL = "Qwen/Qwen2.5-7B-Instruct"

    @modal.web_server(port=VLLM_PORT, startup_timeout=STARTUP_TIMEOUT)
    def serve(self):
        import subprocess
        subprocess.Popen(_vllm_command(self.MODEL))


@app.local_entrypoint()
def main() -> None:
    """Print the endpoint URLs so they can be pasted into ``.env``.

    Runs locally on ``modal deploy`` / ``modal run``; the web URLs resolve to
    the public ``*.modal.run`` hosts Modal assigns to each web server.
    """

    def _url(server) -> str:
        # Recent Modal SDKs expose ``get_web_url()``; older ones use the
        # now-deprecated ``.web_url`` attribute. Prefer the method, fall back.
        serve = server().serve
        getter = getattr(serve, "get_web_url", None)
        if callable(getter):
            return getter()
        return serve.web_url

    print("APEX Modal inference endpoints (append /v1 for LLM_EXTRA_MODELS):")
    print(f"  modal-mistral : {_url(MistralServer)}")
    print(f"  modal-qwen    : {_url(QwenServer)}")
