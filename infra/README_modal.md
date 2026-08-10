# Modal.com GPU Inference

Serverless-GPU inference for APEX TRADER's LLM council. Serves **Mistral 7B**
and **Qwen 7B** as OpenAI-compatible endpoints on Modal A10G GPUs, scaling to
zero when idle so you only pay while a model is warm.

The deploy script (`infra/modal_vllm_deploy.py`) launches vLLM through its
**stable CLI** (`python -m vllm.entrypoints.openai.api_server`) inside a
`@modal.web_server`. There are **no vLLM internal imports**, so it survives
vLLM version bumps.

## Prerequisites

- A [Modal](https://modal.com) account
- Python with the Modal CLI: `pip install modal`

## Deploy

```bash
modal token new                        # one-time browser auth
modal deploy infra/modal_vllm_deploy.py
```

Modal will:

1. Build the vLLM container image (first run ~5 min).
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

`modal` is a keyless, OpenAI-compatible provider — no API key is required.

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
