"""Every harness script, run through a real shell with the tool replaced by an echo.

What reaches the tool is the argv the shell produced, so that is what is checked: a
model name with spaces, quotes and a `$` arrives as one argument and unexpanded, and
the token arrives expanded exactly where it is meant to.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

from pathlib import Path

import pytest

from wingsbench.plan import Cell
from wingsbench.plan import Load
from wingsbench.plan import Model
from wingsbench.plan import SweepSpec
from wingsbench.harnesses import aiperf
from wingsbench.harnesses import guidellm
from wingsbench.harnesses import vllm_bench
from wingsbench.harnesses import vllm_sweep
from wingsbench.harnesses.base import TOKEN_ENV
from wingsbench.harnesses.base import Context
from wingsbench.harnesses.base import HarnessError


pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

TOKEN = "tok-123"
AWKWARD = "org/it's a $HOME `model`"
ECHO = 'printf "%s\\0" "$@"'


def _context(tmp_path: Path) -> Context:
    return Context(
        endpoint_url="http://host.docker.internal:8080/v1 x",
        token=TOKEN,
        hf_cache=tmp_path,
        results_dir=tmp_path,
        image="example.invalid/harness@sha256:" + "0" * 64,
    )


def _cell(name: str = AWKWARD) -> Cell:
    return Cell(
        harness="aiperf",
        model=Model(name=name, tokenizer=name),
        load=Load(concurrency=2, requests=4),
        repeat=1,
        input_tokens=100,
        output_tokens=10,
    )


def _argv(script: str, tool: str) -> list[str]:
    """The argv `tool` would have been called with, after the container's shell."""
    stub = f"{tool}() {{ {ECHO}; }}\n"
    done = subprocess.run(
        ["bash", "-c", stub + script],
        env={"PATH": os.environ.get("PATH", ""), TOKEN_ENV: TOKEN},
        capture_output=True,
        check=True,
    )
    return done.stdout.decode().split("\0")[:-1]


def _script(argv: list[str]) -> str:
    assert argv[-2] == "-c"
    return argv[-1]


def _after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_aiperf(tmp_path: Path) -> None:
    argv = _argv(_script(aiperf.command(_cell(), _context(tmp_path)).argv), "aiperf")
    assert _after(argv, "--model") == AWKWARD
    assert _after(argv, "--tokenizer") == AWKWARD
    assert _after(argv, "--url") == "http://host.docker.internal:8080/v1 x"
    assert _after(argv, "-H") == f"Authorization: Bearer {TOKEN}"


def test_vllm_bench(tmp_path: Path) -> None:
    argv = _argv(_script(vllm_bench.command(_cell(), _context(tmp_path)).argv), "python3")
    assert _after(argv, "--model") == AWKWARD
    assert _after(argv, "--tokenizer") == AWKWARD
    assert _after(argv, "--base-url") == "http://host.docker.internal:8080/v1 x"
    assert _after(argv, "--header") == f"Authorization=Bearer {TOKEN}"


def test_vllm_sweep_bench_command_survives_both_splits(tmp_path: Path) -> None:
    spec = SweepSpec(
        harness="vllm-sweep",
        model=Model(name=AWKWARD, tokenizer=AWKWARD),
        ladder=(1, 2),
        input_tokens=100,
        output_tokens=10,
        requests_per_rung=4,
        seconds_per_rung=0,
    )
    argv = _argv(_script(vllm_sweep.sweep_command(_context(tmp_path), spec).argv), "python3")
    bench = shlex.split(_after(argv, "--bench-cmd"))
    assert _after(bench, "--model") == AWKWARD
    assert _after(bench, "--tokenizer") == AWKWARD
    assert _after(bench, "--header") == f"Authorization=Bearer {TOKEN}"


def test_guidellm_expands_only_the_key(tmp_path: Path) -> None:
    name = "org/it's a $HOME model"
    argv = _argv(_script(guidellm.command(_cell(name), _context(tmp_path)).argv), "guidellm")
    backend = _after(argv, "--backend")
    assert backend == (
        f"kind=openai_http,target=http://host.docker.internal:8080/v1 x,model={name},"
        f"api_key={TOKEN},validate_backend=false"
    )
    assert _after(argv, "--tokenizer") == f"kind=huggingface_auto,model={name}"


def test_guidellm_refuses_a_comma_it_would_read_as_a_separator(tmp_path: Path) -> None:
    with pytest.raises(HarnessError, match="comma"):
        guidellm.command(_cell("org/a,b"), _context(tmp_path))
