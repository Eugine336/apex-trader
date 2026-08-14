# Modal.com GPU Inference

## Recommended: Modal managed inference endpoints

APEX runs on a small VPS (~1.8 GB RAM, no GPU), so all LLM inference must happen
off the box. The **recommended** way to do this is a **Modal managed inference
endpoint**, created from the [Modal dashboard](https://modal.com). Modal hosts a
model for you, serves an OpenAI-compatible `/v1` API, and handles the container
image, GPU, CUDA, and scaling automatically — there is nothing to deploy or
maintain from this repo.

APEX currently uses one managed endpoint serving **Qwen 3.6 35B (A3B)** through
SGLang on a `1xB200`:

- Endpoint URL: `https://<your-workspace>--ep-<endpoint-name>-server.<region>.modal.direct/v1`
  (e.g. `https://eugine336--ep-qwen3-6-35b-a3b-server.ap-south.modal.direct/v1`)
- Served model name: `Qwen/Qwen3.6-35B-A3B`
- Auth: a bearer token of the form `TokenID.TokenSecret`, sent as
  `Authorization: Bearer <token>`.

Note the managed-endpoint URL uses the `.modal.direct` domain with an `ep-`
prefix, a `-server` suffix, and the deployment region (e.g. `ap-south`) — it is
**not** the `*.modal.run` form produced by the custom deploy script below. Copy
the exact URL from the Modal dashboard and append `/v1`:

```text
https://USERNAME--ENDPOINT-NAME-server.REGION.modal.direct/v1
```

The `model` field in `LLM_EXTRA_MODELS` must exactly match the endpoint's
`--served-model-name`. In Modal's managed endpoint UI, check the Source tab for
the served name instead of guessing from the Hugging Face repository name.

### Wire it into `.env`

Add one entry to `LLM_EXTRA_MODELS` with `provider:"modal"`, the endpoint
`base_url` (ending in `/v1`), and the combined bearer token as `api_key`:

```json
{"name":"modal-qwen35b","provider":"modal","model":"Qwen/Qwen3.6-35B-A3B","base_url":"https://eugine336--ep-qwen3-6-35b-a3b-server.ap-south.modal.direct/v1","api_key":"<TokenID>.<TokenSecret>","tier":2,"timeout_seconds":300,"classes":["deep"]}
```

Also set the same token as `MODAL_INFERENCE_KEY` in `.env`. APEX sends the
`api_key` as `Authorization: Bearer <token>` on every request (see
`llm/client.py`), so an endpoint with `REQUIRE_AUTHENTICATION=True`
authenticates automatically. Keep `timeout_seconds` at `300` or higher when the
endpoint scales to zero (`min_containers=0`, `scaledown_window=300`), because the
first request after idle must tolerate the cold start. Unauthenticated Modal
endpoints can omit `api_key`, but authenticated managed endpoints need either
the entry-level `api_key` or `MODAL_INFERENCE_KEY`.

---

## Deprecated: custom vLLM deploy script

> **DEPRECATED.** The custom deploy script below
> ([`infra/modal_vllm_deploy.py`](modal_vllm_deploy.py)) kept crashing
> (deprecated Modal APIs, missing CUDA toolkit, FlashInfer JIT failures) and has
> been superseded by the managed inference endpoint documented above. It is kept
> only as a reference; prefer a managed endpoint for anything new.

[`infra/modal_vllm_deploy.py`](modal_vllm_deploy.py) deploys two
OpenAI-compatible inference endpoints on Modal's serverless GPUs — one for
**Mistral 7B** and one for **Qwen 7B**, each served by vLLM on an A10G. The VPS
only makes HTTP calls; Modal spins a GPU up on demand and scales it back to zero
when idle, so you pay only for warm time.

The deploy script (`infra/modal_vllm_deploy.py`) launches vLLM through its
**stable CLI** (`python -m vllm.entrypoints.openai.api_server`) inside a
`@modal.web_server`. There are **no vLLM internal imports**, so it survives
vLLM version bumps.

## Container image

The image is vLLM's **official Docker image**, pinned to a stable tag:

```python
modal.Image.from_registry("vllm/vllm-openai:v0.8.5.post1", add_python="3.11")
```

This ships CUDA (`nvcc`), pre-compiled FlashInfer, and vLLM itself. Using
`debian_slim` + `pip install vllm` instead fails at runtime with
`Could not find nvcc ...` because FlashInfer tries to JIT-compile CUDA kernels
in an image with no CUDA toolkit. Pin a specific tag (never `latest`) to avoid
surprise breakage on upstream releases.

## Prerequisites

- A [Modal](https://modal.com) account
- Python with the Modal CLI: `pip install modal`

## Deploy

```bash
modal token new                        # one-time browser auth
modal deploy infra/modal_vllm_deploy.py
```

Modal will:

1. Pull the vLLM image (first run only).
2. Download the models into the persistent `apex-model-cache` volume (first run
   ~10 min).
3. Print the public URLs, e.g.:

   ```
   https://<workspace>--apex-trader-llm-mistralserver-serve.modal.run
   https://<workspace>--apex-trader-llm-qwenserver-serve.modal.run
   ```

`startup_timeout=300` gives each container up to 5 minutes to load its model on
a cold start.

## Wire into APEX

Add the endpoints to `LLM_EXTRA_MODELS` in `.env`, appending `/v1` to make them
OpenAI-compatible base URLs (replace `<workspace>` with your Modal workspace):

```
{"name":"modal-mistral","provider":"modal","model":"mistralai/Mistral-7B-Instruct-v0.3","base_url":"https://<workspace>--apex-trader-llm-mistralserver-serve.modal.run/v1","tier":2,"timeout_seconds":60}
{"name":"modal-qwen","provider":"modal","model":"Qwen/Qwen2.5-7B-Instruct","base_url":"https://<workspace>--apex-trader-llm-qwenserver-serve.modal.run/v1","tier":2,"timeout_seconds":60}
```

Those deprecated custom endpoints were unauthenticated, so no API key was
required for that setup.

## Design notes

- **`@modal.web_server(port=8000, startup_timeout=300)`** — runs vLLM as a
  subprocess on port 8000; Modal proxies it to the public URL.
- **No `@modal.enter()`** and **no custom server builder** — the web_server
  pattern needs neither.
- **`gpu="a10g"`** (string form) and **`scaledown_window`** match the current
  Modal SDK.
- **Optional auth** can be layered on later with a Modal secret and a bearer
  token; it is intentionally omitted here to keep the deploy minimal.

## Costs

Each A10G container costs roughly **$0.60/hr while warm** and scales to zero
after 5 minutes of no requests (`SCALEDOWN_WINDOW = 300`). Cold start after idle
is ~30-60 seconds on the first call.
