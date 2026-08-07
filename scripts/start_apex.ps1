<#
    APEX TRADER — one-command startup (Windows / PowerShell).

    Brings the whole stack up as a single unit:
      1. Ensures the Ollama daemon is running and the council's local models are
         pulled (llama3.1 / qwen2.5 / deepseek-r1 / mistral / gemma2).
      2. Optionally starts a vLLM server (:8002) and a self-hosted NVIDIA NIM
         (:8001) when you provide a launch command (GPU boxes only).
      3. Waits until each configured local endpoint answers, then launches APEX.

    Ports (must not collide): dashboard :8000, NIM :8001, vLLM :8002,
    Ollama :11434. These match .env's LLM_EXTRA_MODELS roster.

    Usage (from the repo root):
        powershell -ExecutionPolicy Bypass -File scripts\start_apex.ps1
        powershell -ExecutionPolicy Bypass -File scripts\start_apex.ps1 -Dashboard
        # GPU box, also bring up vLLM + NIM:
        $env:APEX_VLLM_CMD = 'vllm serve meta-llama/Llama-3.1-8B-Instruct --port 8002'
        $env:APEX_NIM_CMD  = 'docker run --gpus all -p 8001:8000 nvcr.io/nim/meta/llama-3.1-8b-instruct:latest'
        powershell -ExecutionPolicy Bypass -File scripts\start_apex.ps1

    To have it come up on machine boot: add this script as a Task Scheduler task
    "At log on" (or wrap it in a Windows service). Everything is best-effort — a
    missing runtime is logged and skipped; APEX still starts (cloud providers +
    whatever locals are up), and never blocks on a model download already present.
#>

[CmdletBinding()]
param(
    [switch]$Dashboard,
    # Max seconds to wait for each local endpoint to become healthy.
    [int]$HealthTimeoutSeconds = 120
)

$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# The Ollama models the council roster expects (keep in sync with .env).
$OllamaModels = @("llama3.1", "qwen2.5", "deepseek-r1", "mistral", "gemma2")

function Write-Step($msg) { Write-Host "[start-apex] $msg" -ForegroundColor Cyan }

function Test-Endpoint($url) {
    try {
        Invoke-WebRequest -Uri $url -TimeoutSec 3 -UseBasicParsing | Out-Null
        return $true
    } catch {
        # A 4xx (e.g. 404 on a probe path) still proves the server is listening.
        if ($_.Exception.Response -ne $null) { return $true }
        return $false
    }
}

function Wait-Healthy($name, $url, $timeout) {
    $deadline = (Get-Date).AddSeconds($timeout)
    while ((Get-Date) -lt $deadline) {
        if (Test-Endpoint $url) { Write-Step "$name is healthy ($url)"; return $true }
        Start-Sleep -Seconds 2
    }
    Write-Step "WARNING: $name did not become healthy within ${timeout}s ($url) — APEX will run without it"
    return $false
}

# ── 1. Ollama ────────────────────────────────────────────────────────────────
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if ($ollama) {
    if (-not (Test-Endpoint "http://localhost:11434/api/tags")) {
        Write-Step "Starting Ollama daemon..."
        Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
    }
    Wait-Healthy "Ollama" "http://localhost:11434/api/tags" 30 | Out-Null
    foreach ($m in $OllamaModels) {
        Write-Step "Ensuring Ollama model present: $m (pull is a no-op if already downloaded)"
        & ollama pull $m
    }
} else {
    Write-Step "Ollama not found on PATH — install from https://ollama.com to run the local council models. Skipping."
}

# ── 2. vLLM (optional — GPU box; set $env:APEX_VLLM_CMD) ───────────────────────
if ($env:APEX_VLLM_CMD) {
    if (-not (Test-Endpoint "http://localhost:8002/v1/models")) {
        Write-Step "Starting vLLM: $($env:APEX_VLLM_CMD)"
        Start-Process -FilePath "powershell" -ArgumentList "-NoProfile","-Command",$env:APEX_VLLM_CMD -WindowStyle Hidden
    }
    Wait-Healthy "vLLM" "http://localhost:8002/v1/models" $HealthTimeoutSeconds | Out-Null
} else {
    Write-Step "APEX_VLLM_CMD not set — skipping vLLM (needs a GPU). Set it to auto-start vLLM on :8002."
}

# ── 3. Self-hosted NVIDIA NIM (optional — GPU box; set $env:APEX_NIM_CMD) ──────
if ($env:APEX_NIM_CMD) {
    if (-not (Test-Endpoint "http://localhost:8001/v1/models")) {
        Write-Step "Starting NVIDIA NIM: $($env:APEX_NIM_CMD)"
        Start-Process -FilePath "powershell" -ArgumentList "-NoProfile","-Command",$env:APEX_NIM_CMD -WindowStyle Hidden
    }
    Wait-Healthy "NVIDIA NIM" "http://localhost:8001/v1/models" $HealthTimeoutSeconds | Out-Null
} else {
    Write-Step "APEX_NIM_CMD not set — skipping self-hosted NIM (needs a GPU). Set it to auto-start NIM on :8001."
}

# ── 4. Launch APEX ─────────────────────────────────────────────────────────────
Write-Step "Local runtimes ready — launching APEX..."
if ($Dashboard) {
    python main.py --dashboard
} else {
    python main.py
}
