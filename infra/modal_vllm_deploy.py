@'
"""APEX TRADER - Modal.com serverless-GPU inference."""
from __future__ import annotations
import os, subprocess
import modal

app = modal.App("apex-trader-llm")
vllm_image = modal.Image.debian_slim(python_version="3.11").pip_install("vllm")
model_cache = modal.Volume.from_name("apex-model-cache", create_if_missing=True)
CACHE_DIR = "/root/.cache/huggingface"
SCALEDOWN_WINDOW = 300

@app.cls(image=vllm_image, gpu="a10g", volumes={CACHE_DIR: model_cache}, scaledown_window=SCALEDOWN_WINDOW)
class MistralServer:
    @modal.web_server(port=8000, startup_timeout=300)
    def serve(self):
        subprocess.Popen(["python", "-m", "vllm.entrypoints.openai.api_server",
            "--model", "mistralai/Mistral-7B-Instruct-v0.3",
            "--host", "0.0.0.0", "--port", "8000", "--gpu-memory-utilization", "0.90"])

@app.cls(image=vllm_image, gpu="a10g", volumes={CACHE_DIR: model_cache}, scaledown_window=SCALEDOWN_WINDOW)
class QwenServer:
    @modal.web_server(port=8000, startup_timeout=300)
    def serve(self):
        subprocess.Popen(["python", "-m", "vllm.entrypoints.openai.api_server",
            "--model", "Qwen/Qwen2.5-7B-Instruct",
            "--host", "0.0.0.0", "--port", "8000", "--gpu-memory-utilization", "0.90"])
'@ | Set-Content infra/modal_vllm_deploy.py
