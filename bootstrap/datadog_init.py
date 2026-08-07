import os
import sys


def _initialise_datadog() -> None:
    if not os.getenv("DD_API_KEY"):
        print("[datadog] DD_API_KEY not set — Datadog APM disabled", file=sys.stderr)
        return

    try:
        os.environ.setdefault("DD_LOGS_INJECTION", "true")

        from ddtrace import patch, patch_all, tracer

        patch_all()
        patch(loguru=True)
        tracer.configure(
            hostname=os.getenv("DD_AGENT_HOST", "localhost"),
            port=int(os.getenv("DD_TRACE_AGENT_PORT", 8126)),
        )

        service = os.getenv("DD_SERVICE", "apex-trader")
        env = os.getenv("DD_ENV", "production")
        print(f"[datadog] Initialised (service={service} env={env})", file=sys.stderr)
    except Exception as exc:
        print(f"[datadog] Datadog initialisation skipped: {exc}", file=sys.stderr)


_initialise_datadog()
