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

Each model is served by vLLM's built-in OpenAI-compatible FastAPI app, which
implements ``/v1/chat/completions``, ``/v1/completions`` and ``/v1/models``
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
Then set the same value as ``MODAL_INFERENCE_KEY`` in ``.env`` so APEX sends it
as ``Authorization: Bearer <token>``. Leave the secret unset to run open (the
endpoints are still obscured by an unguessable Modal URL).
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
# ``_build_server`` reads MODAL_INFERENCE_KEY from the environment and, when set,
# enforces ``Authorization: Bearer <token>`` on every request. It is NOT wired
# by default: ``Secret.from_name`` is validated at deploy time and would abort
# the deploy with "Secret not found" if the secret has not been created yet.


def _build_server(model_name: str):
    """Return a vLLM OpenAI-compatible ASGI app for ``model_name``.

    vLLM ships a FastAPI app that speaks the OpenAI REST API. We construct its
    engine from the model, then optionally wrap it with a bearer-token check
    driven by the ``MODAL_INFERENCE_KEY`` env var (populated from the Modal
    secret when present).
    """
    from vllm.engine.arg_utils import AsyncEngineArgs
    from vllm.engine.async_llm_engine import AsyncLLMEngine
    from vllm.entrypoints.openai.api_server import build_app
    from vllm.entrypoints.openai.serving_engine import BaseModelPath
    from vllm.entrypoints.openai.serving_chat import OpenAIServingChat
    from vllm.entrypoints.openai.serving_completion import OpenAIServingCompletion

    engine_args = AsyncEngineArgs(model=model_name, gpu_memory_utilization=0.90)
    engine = AsyncLLMEngine.from_engine_args(engine_args)

    fastapi_app = build_app(engine)

    required_key = str(os.environ.get("MODAL_INFERENCE_KEY") or "").strip()
    if required_key:
        from fastapi import Request
        from fastapi.responses import JSONResponse

        @fastapi_app.middleware("http")
        async def _require_bearer(request: Request, call_next):
            header = request.headers.get("authorization", "")
            if header != f"Bearer {required_key}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    return fastapi_app


# ── Mistral 7B endpoint ───────────────────────────────────────────────────────
@app.cls(
    image=vllm_image,
    gpu="a10g",
    volumes={CACHE_DIR: model_cache},
    scaledown_window=SCALEDOWN_WINDOW,
)
class MistralServer:
    MODEL = "mistralai/Mistral-7B-Instruct-v0.3"

    @modal.enter()
    def load(self) -> None:
        # Cold-start model load (cached on the Volume after the first download).
        self._app = _build_server(self.MODEL)

    @modal.asgi_app()
    def serve(self):
        return self._app


# ── Qwen 7B endpoint ──────────────────────────────────────────────────────────
@app.cls(
    image=vllm_image,
    gpu="a10g",
    volumes={CACHE_DIR: model_cache},
    scaledown_window=SCALEDOWN_WINDOW,
)
class QwenServer:
    MODEL = "Qwen/Qwen2.5-7B-Instruct"

    @modal.enter()
    def load(self) -> None:
        self._app = _build_server(self.MODEL)

    @modal.asgi_app()
    def serve(self):
        return self._app


@app.local_entrypoint()
def main() -> None:
    """Print the endpoint URLs so they can be pasted into ``.env``.

    Runs locally on ``modal deploy`` / ``modal run``; the web URLs resolve to
    the public ``*.modal.run`` hosts Modal assigns to each ASGI app.
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
