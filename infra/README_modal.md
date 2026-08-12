# Modal.com GPU inference for APEX's LLM council

## Recommended: Modal managed inference endpoints

APEX runs on a small VPS (~1.8 GB RAM, no GPU), so all LLM inference must happen
off the box. The **recommended** way to do this is a **Modal managed inference
endpoint**, created from the [Modal dashboard](https://modal.com). Modal hosts a
model for you, serves an OpenAI-compatible `/v1` API, and handles the container
image, GPU, CUDA, and scaling automatically — there is nothing to deploy or
maintain from this repo.

APEX currently uses one managed endpoint serving **Qwen 3.6 35B (A3B)** on a
`1xB200`:

- Endpoint URL: `https://<your-workspace>--ep-<endpoint-name>-server.<region>.modal.direct/v1`
  (e.g. `https://eugine336--ep-qwen3-6-35b-a3b-server.ap-south.modal.direct/v1`)
- Auth: a bearer token of the form `TokenID.TokenSecret`, sent as
  `Authorization: Bearer <token>`.

Note the managed-endpoint URL uses the `.modal.direct` domain with an `ep-`
prefix, a `-server` suffix, and the deployment region (e.g. `ap-south`) — it is
**not** the `*.modal.run` form produced by the custom deploy script below. Copy
the exact URL from the Modal dashboard and append `/v1`.

### Wire it into `.env`

Add one entry to `LLM_EXTRA_MODELS` with `provider:"modal"`, the endpoint
`base_url` (ending in `/v1`), and the combined bearer token as `api_key`:

```json
{"name":"modal-qwen35b","provider":"modal","model":"Qwen/Qwen3.6-35B","base_url":"https://eugine336--ep-qwen3-6-35b-a3b-server.ap-south.modal.direct/v1","api_key":"<TokenID>.<TokenSecret>","tier":2,"timeout_seconds":90,"classes":["deep"]}
```

Also set the same token as `MODAL_INFERENCE_KEY` in `.env`. APEX sends the
`api_key` as `Authorization: Bearer <token>` on every request (see
`llm/client.py`), so the endpoint authenticates automatically. Because a managed
endpoint always requires the token, keep `api_key` populated on this entry even
though `provider:"modal"` is otherwise treated as keyless.

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
