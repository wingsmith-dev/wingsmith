"""The two properties every probe must have, checked against a stub that can misbehave.

`test_noisy_twins_agree` is the one that matters. It runs the whole probe set against
two endpoints that are conforming, identical in behaviour, and different in every way
that does not matter — token counts, response ids, chunk counts, trailing whitespace.
A probe that puts any of those in its `compare` tuple shows up here as a difference,
and that is a defect: against real endpoints the same probe would report a difference
on every run and the report would be noise.

The single expected difference is `tokens.prompt_accounting`, which compares reported
prompt token counts on purpose. Two endpoints serving one model must tokenize
identically, so there the count *is* the answer — and the noisy stub, which randomises
it, is exactly the defect that probe exists to catch.

`test_planted_defects_are_found` is the other direction: three specific defects, all
three caught, and nothing else reported.
"""

from __future__ import annotations

import os
import sys
import time
import socket
import subprocess

from pathlib import Path

import httpx
import pytest

from hgrconform.facts import compare
from hgrconform.store import Run
from hgrconform.store import Store
from hgrconform.probes import registry
from hgrconform.runner import run_probes
from hgrconform.profile import Profile


STUB = Path(__file__).parent / "stub.py"
TOKENIZER_PROBE = "tokens.prompt_accounting"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def stub():
    """Start a stub in a given mode and wait for it to answer, yielding its base URL."""
    started: list[subprocess.Popen] = []

    def start(quirk: str, seed: int = 0) -> str:
        port = _free_port()
        process = subprocess.Popen(
            [sys.executable, str(STUB), str(port)],
            env=os.environ | {"QUIRK": quirk, "SEED": str(seed)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        started.append(process)
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                httpx.get(f"{url}/v1/models", timeout=0.5)
                return url
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError(f"stub on {port} did not start")

    yield start
    for process in started:
        process.terminate()
        process.wait(timeout=5)


def _profile(name: str, url: str, needles: tuple[str, ...] = ()) -> Profile:
    """The stub, described the way a committed profile describes a real endpoint.

    It claims the Messages surface because the stub answers it, and a probe that
    `requires` a protocol the profile does not claim is skipped rather than run —
    which would leave the probes that ask about that surface untested here.
    """
    return Profile(
        name=name,
        base_url=url,
        model="gpt-oss-120b",
        token_env="HGRCONFORM_TEST_TOKEN",
        context_window=4096,
        protocols=("chat_completions", "messages"),
        leak_needles=needles,
    )


def _differences(tmp_path: Path, target: Profile, reference: Profile) -> list:
    registry.load()
    probes = registry.select()
    store = Store(tmp_path, Run(run_id="test", started_at="", probe_ids=[p.id for p in probes]))
    run_probes(probes, target, "target", store, verbose=False)
    run_probes(probes, reference, "reference", store, verbose=False)

    found = []
    for probe in probes:
        here = store.run.endpoints["target"].facts[probe.id]
        there = store.run.endpoints["reference"].facts[probe.id]
        if not probe.compare or (here.status == "skipped" and there.status == "skipped"):
            continue
        found.extend(compare(here, there, probe.compare))
    return found


def test_no_probe_raises(tmp_path, stub, monkeypatch):
    """Every probe runs to completion against an endpoint that supports almost nothing."""
    monkeypatch.setenv("HGRCONFORM_TEST_TOKEN", "stub")
    registry.load()
    probes = registry.select()
    store = Store(tmp_path, Run(run_id="t", started_at="", probe_ids=[p.id for p in probes]))
    run_probes(probes, _profile("plain", stub("none")), "target", store, verbose=False)

    errored = [fact.probe for fact in store.run.endpoints["target"].facts.values() if fact.status == "error"]
    assert not errored, f"probes raised: {errored}"


def test_noisy_twins_agree(tmp_path, stub, monkeypatch):
    """Two honest endpoints differ only where a probe compares something it should not."""
    monkeypatch.setenv("HGRCONFORM_TEST_TOKEN", "stub")
    target = _profile("noisy-a", stub("noisy", seed=7))
    reference = _profile("noisy-b", stub("noisy", seed=999))

    unexpected = [d for d in _differences(tmp_path, target, reference) if d.probe != TOKENIZER_PROBE]
    assert not unexpected, "compare keys holding run-varying data: " + ", ".join(
        f"{d.label} ({d.target!r} vs {d.reference!r})" for d in unexpected
    )


def test_planted_defects_are_found(tmp_path, stub, monkeypatch):
    """The three defects the leaky stub carries are all reported, and nothing else is."""
    monkeypatch.setenv("HGRCONFORM_TEST_TOKEN", "stub")
    target = _profile("leaky", stub("leaky"), needles=("hangar", "vllm"))
    reference = _profile("clean", stub("none"))

    labels = {d.label for d in _differences(tmp_path, target, reference)}
    assert "streaming.usage_location.usage_position" in labels
    assert "structured.json_schema.schema_valid" in labels
    assert labels == {
        "streaming.usage_location.usage_position",
        "structured.json_schema.schema_valid",
    }, f"unexpected differences: {labels}"
