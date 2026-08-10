"""APEX TRADER - Modal.com serverless-GPU inference.

Serves Mistral 7B and Qwen 7B as OpenAI-compatible endpoints on Modal A10G
GPUs via vLLM's stable CLI (``python -m vllm.entrypoints.openai.api_server``),
which does not change across vLLM versions.

The container image is vLLM's official Docker image, which ships CUDA (nvcc),
pre-compiled FlashInfer, and vLLM itself — so no JIT CUDA compilation happens
at runtime. Pinned to a stable tag (not ``latest``) to avoid future breakage.

Deploy:
    pip install modal
    modal token new                       # one-time browser auth
    modal deploy infra/modal_vllm_deploy.py

Modal prints the public URLs, e.g.:
    https://<workspace>--apex-trader-llm-mistralserver-serve.modal.run
    https://<workspace>--apex-trader-llm-qwenserver-serve.modal.run

Wire them into ``.env`` (LLM_EXTRA_MODELS) with ``/v1`` appended as the
OpenAI-compatible base_url. Containers scale to zero after SCALEDOWN_WINDOW
seconds of inactivity; cold start is ~30-60s.
"""
from __future__ import annotations

import subprocess

import modal

app = modal.App("apex-trader-llm")
vllm_image = modal.Image.from_registry(
    "vllm/vllm-openai:v0.8.5.post1", add_python="3.11"
)
model_cache = modal.Volume.from_name("apex-model-cache", create_if_missing=True)
CACHE_DIR = "/root/.cache/huggingface"
SCALEDOWN_WINDOW = 300


@app.cls(
    image=vllm_image,
    gpu="a10g",
    volumes={CACHE_DIR: model_cache},
    scaledown_window=SCALEDOWN_WINDOW,
)
class MistralServer:
    @modal.web_server(port=8000, startup_timeout=300)
    def serve(self):
        subprocess.Popen([
            "python", "-m", "vllm.entrypoints.openai.api_server",
            "--model", "mistralai/Mistral-7B-Instruct-v0.3",
            "--host", "0.0.0.0", "--port", "8000",
            "--gpu-memory-utilization", "0.90",
        ])


@app.cls(
    image=vllm_image,
    gpu="a10g",
    volumes={CACHE_DIR: model_cache},
    scaledown_window=SCALEDOWN_WINDOW,
)
class QwenServer:
    @modal.web_server(port=8000, startup_timeout=300)
    def serve(self):
        subprocess.Popen([
            "python", "-m", "vllm.entrypoints.openai.api_server",
            "--model", "Qwen/Qwen2.5-7B-Instruct",
            "--host", "0.0.0.0", "--port", "8000",
            "--gpu-memory-utilization", "0.90",
        ])
