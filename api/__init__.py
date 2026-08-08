"""APEX TRADER — Multi-tenant API package.

A FastAPI control plane that lets multiple users each run their own isolated
APEX trading instance (Option A: process-per-user). The trading engine itself
is untouched — the API spawns it as a subprocess with per-user environment
(broker credentials, data/log directories, config path).
"""
