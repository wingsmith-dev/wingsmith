"""The one-run plan `just bench` hands to wingsbench, written from BENCH_* variables.

A committed plan is the right shape for a suite and the wrong one for "how does this endpoint do
at concurrency 32, right now". This writes that question as a plan, so the quick check runs
through the same runner, image pinning and report as `just bench-suite` instead of a second copy
of them.

    BENCH_MODEL        the model name the endpoint serves (required)
    BENCH_TOKENIZER    the Hugging Face repository that counts its tokens (default: BENCH_MODEL)
    BENCH_URL          resolved inside the container (default: http://host.docker.internal:8080)
    BENCH_CONCURRENCY  requests in flight (default: 10)
    BENCH_REQUESTS     requests in total (default: 100)
    BENCH_ISL          input tokens per request (default: 2000)
    BENCH_OSL          output tokens per request (default: 256)
    BENCH_TOKEN        sent as a bearer token when set; read by wingsbench, never written here
"""

from __future__ import annotations

import os
import sys
import json

from collections.abc import Mapping


def _quote(value: str) -> str:
    # A JSON string is a valid TOML basic string.
    return json.dumps(value)


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name) or str(default)
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a whole number, not {raw!r}") from None
    if value < 1:
        raise SystemExit(f"{name} must be at least 1")
    return value


def plan(env: Mapping[str, str]) -> str:
    model = env.get("BENCH_MODEL", "").strip()
    if not model:
        raise SystemExit("set BENCH_MODEL to the model name the endpoint serves")
    tokenizer = env.get("BENCH_TOKENIZER", "").strip() or model
    url = env.get("BENCH_URL", "").strip() or "http://host.docker.internal:8080"
    concurrency = _int(env, "BENCH_CONCURRENCY", 10)
    requests = _int(env, "BENCH_REQUESTS", 100)
    isl = _int(env, "BENCH_ISL", 2000)
    osl = _int(env, "BENCH_OSL", 256)
    return "\n".join(
        [
            'name = "bench"',
            f"description = {_quote(f'One AIPerf run of {model} at concurrency {concurrency}, from this machine.')}",
            'harnesses = ["aiperf"]',
            "repeats = 1",
            "cooldown_seconds = 0",
            f"input_tokens = {isl}",
            f"output_tokens = {osl}",
            "",
            "[endpoint]",
            f"url = {_quote(url)}",
            'token_env = "BENCH_TOKEN"',
            "",
            "[[models]]",
            f"name = {_quote(model)}",
            f"tokenizer = {_quote(tokenizer)}",
            "",
            "[[load]]",
            f"concurrency = {concurrency}",
            f"requests = {requests}",
            "",
            "[sweep]",
            "enabled = false",
            "",
            "[knee]",
            "enabled = false",
            "",
        ]
    )


if __name__ == "__main__":
    sys.stdout.write(plan(os.environ))
