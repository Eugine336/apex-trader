# Modal.com GPU inference for APEX's LLM council

APEX runs on a small VPS (~1.8 GB RAM, no GPU), so all LLM inference must happen
off the box. [`infra/modal_vllm_deploy.py`](modal_vllm_deploy.py) deploys two
OpenAI-compatible inference endpoints on Modal's serverless GPUs — one for
**Mistral 7B** and one for **Qwen 7B**, each served by vLLM on an A10G. The VPS
only makes HTTP calls; Modal spins a GPU up on demand and scales it back to zero
when idle, so you pay only for warm time.

## 1. Install Modal

```bash
pip install modal
```

## 2. Authenticate (one-time)

```bash
modal token new    # opens a browser to link your Modal account
```

## 3. Deploy

```bash
modal deploy infra/modal_vllm_deploy.py
```

Modal prints the public endpoint URLs, e.g.

```
https://<your-workspace>--apex-trader-llm-mistral-serve.modal.run
https://<your-workspace>--apex-trader-llm-qwen-serve.modal.run
```

## 4. Wire the URLs into `.env`

In `LLM_EXTRA_MODELS`, replace `YOUR_USERNAME` in the two `modal-*` entries with
your Modal workspace name and append `/v1` to each URL, so `base_url` looks like:

```
https://<your-workspace>--apex-trader-llm-mistral-serve.modal.run/v1
https://<your-workspace>--apex-trader-llm-qwen-serve.modal.run/v1
```

No API key is required — Modal authenticates deploys via `modal token new`, and
the served endpoints are keyless by default.

## 5. Optional: bearer-token auth

For an extra layer beyond the unguessable `*.modal.run` URL, arm a bearer token:

```bash
modal secret create apex-inference-key MODAL_INFERENCE_KEY=<random-token>
```

Then set the same value in `.env`:

```
MODAL_INFERENCE_KEY=<random-token>
```

APEX will send it as `Authorization: Bearer <random-token>`; the deploy adds a
matching check to each endpoint. Leave both unset to run open.

## Cost

An A10G is roughly **$0.60/hr**, billed only while a container is warm. With the
default `container_idle_timeout=300` (5 min), a burst of council calls keeps the
GPU warm for the window, then it scales to zero and billing stops.
