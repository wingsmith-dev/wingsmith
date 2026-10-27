"""The two properties every probe must have, checked against a stub that can misbehave.

`test_noisy_twins_agree` is the one that matters. It runs the whole probe set against
two endpoints that are conforming, identical in behaviour, and different in every way
that does not matter: token counts, response ids, chunk counts, trailing whitespace,
and the model's own single-sample decisions. A probe that puts any of those in its
`compare` tuple shows up here as a difference, and that is a defect: against real
endpoints the same probe would report a difference on every run and the report would
be noise.

The stub claims every modality, the Messages surface and a thinking switch, and the
heavy probes run too, so no probe is left out of either check by a skip.

`test_planted_defects_are_found` is the other direction: the leaky stub's defects are
all caught, and nothing else is reported.
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
from hgrconform.profile import Thinking
from hgrconform.probes.eos import MIN_SAMPLES
from hgrconform.probes.eos import SAMPLES_ENV


STUB = Path(__file__).parent / "stub.py"
NEEDLES = ("hangar", "vllm")


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


def _profile(name: str, url: str) -> Profile:
    """The stub, described the way a committed profile describes a real endpoint.

    It claims everything the stub serves, because a probe that `requires` a capability
    the profile does not claim is skipped instead of run, which would leave it out of
    the self-check entirely.
    """
    return Profile(
        name=name,
        base_url=url,
        model="gpt-oss-120b",
        token_env="HGRCONFORM_TEST_TOKEN",
        context_window=4096,
        modalities=("text", "image", "audio", "video"),
        protocols=("chat_completions", "messages"),
        leak_needles=NEEDLES,
        thinking=Thinking(syntax="chat_template", default_on=False, efforts=("low", "high")),
    )


def _all_probes() -> list[registry.Probe]:
    registry.load()
    return registry.select(skip_heavy=False)


def _run(tmp_path: Path, target: Profile, reference: Profile) -> Store:
    probes = _all_probes()
    store = Store(tmp_path, Run(run_id="test", started_at="", probe_ids=[p.id for p in probes]))
    run_probes(probes, target, "target", store, verbose=False)
    run_probes(probes, reference, "reference", store, verbose=False)
    return store


def _differences(store: Store) -> list:
    probes = [registry.get(probe_id) for probe_id in store.run.probe_ids]
    found = []
    for probe in probes:
        here = store.run.endpoints["target"].facts[probe.id]
        there = store.run.endpoints["reference"].facts[probe.id]
        if not probe.compare or (here.status == "skipped" and there.status == "skipped"):
            continue
        found.extend(compare(here, there, probe.compare))
    return found


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("HGRCONFORM_TEST_TOKEN", "stub")
    monkeypatch.setenv(SAMPLES_ENV, str(MIN_SAMPLES))


def test_every_probe_runs_and_none_raises(tmp_path, stub):
    """Every probe runs to completion, and none is skipped, against an endpoint that claims everything."""
    probes = _all_probes()
    store = Store(tmp_path, Run(run_id="t", started_at="", probe_ids=[p.id for p in probes]))
    run_probes(probes, _profile("plain", stub("none")), "target", store, verbose=False)

    facts = store.run.endpoints["target"].facts.values()
    errored = {fact.probe: fact.error.splitlines()[0] for fact in facts if fact.status == "error"}
    skipped = {fact.probe: fact.notes for fact in facts if fact.status == "skipped"}
    assert not errored, f"probes raised: {errored}"
    assert not skipped, f"probes skipped: {skipped}"


def test_noisy_twins_agree(tmp_path, stub):
    """Two conforming endpoints differ only where a probe compares something it should not."""
    store = _run(tmp_path, _profile("noisy-a", stub("noisy", seed=7)), _profile("noisy-b", stub("noisy", seed=1000)))

    unexpected = _differences(store)
    assert not unexpected, "compare keys holding run-varying data: " + ", ".join(
        f"{d.label} ({d.target!r} vs {d.reference!r})" for d in unexpected
    )


def test_planted_defects_are_found(tmp_path, stub):
    """The defects the leaky stub carries are all reported, and nothing else is."""
    store = _run(tmp_path, _profile("leaky", stub("leaky")), _profile("clean", stub("none")))

    labels = {d.label for d in _differences(store)}
    assert labels == {
        "streaming.usage_location.usage_position",
        "video.streaming.usage_position",
        "structured.json_schema.schema_valid",
    }, f"unexpected differences: {labels}"

    # Not diffed, because the reference cannot answer it, so checked on each side.
    leaky = store.run.endpoints["target"].facts["errors.leakage"]
    clean = store.run.endpoints["reference"].facts["errors.leakage"]
    assert leaky.observed["leaked"] is True
    assert set(leaky.observed["needles_found"]) == set(NEEDLES)
    assert clean.observed["leaked"] is False
