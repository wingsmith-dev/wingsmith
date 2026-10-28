"""Diffing a stored run: nothing missing is hidden, and an old run still loads."""

from __future__ import annotations

import json

from pathlib import Path

import pytest

from wingsconform.cli import TARGET
from wingsconform.cli import REFERENCE
from wingsconform.cli import compare_run
from wingsconform.facts import Fact
from wingsconform.store import Run
from wingsconform.store import Store
from wingsconform.store import Endpoint
from wingsconform.store import read_run
from wingsconform.probes import registry
from wingsconform.runner import run_probes
from wingsconform.profile import Profile


PROBE = "basics.completion"


def _run(target: dict[str, Fact], reference: dict[str, Fact], probe_ids: list[str]) -> Run:
    registry.load()
    run = Run(run_id="t", started_at="", probe_ids=probe_ids)
    run.endpoints[TARGET] = Endpoint(TARGET, "a", "http://a", "m", facts=target)
    run.endpoints[REFERENCE] = Endpoint(REFERENCE, "b", "http://b", "m", facts=reference)
    return run


def _observed(probe: str = PROBE, **observed: object) -> Fact:
    return Fact(probe=probe, status="observed", observed=dict(observed))


def test_a_probe_missing_on_one_side_is_a_not_run_difference() -> None:
    run = _run({PROBE: _observed(status=200)}, {}, [PROBE])
    [difference] = compare_run(run).differences
    assert (difference.probe, difference.kind) == (PROBE, "not-run")
    assert (difference.target, difference.reference) == ("observed", "absent")


def test_a_probe_unknown_to_this_version_is_listed_not_dropped() -> None:
    gone = "retired.probe"
    run = _run({gone: _observed(gone)}, {gone: _observed(gone)}, [gone])
    result = compare_run(run)
    assert result.unknown == [gone]
    assert result.differences == []


def test_a_run_without_a_reference_is_a_value_error() -> None:
    run = _run({}, {}, [])
    del run.endpoints[REFERENCE]
    with pytest.raises(ValueError):
        compare_run(run)


def test_an_unknown_probe_id_raises_a_lookup_error() -> None:
    registry.load()
    with pytest.raises(LookupError):
        registry.get("no.such.probe")


def test_a_run_written_by_another_version_still_loads(tmp_path: Path) -> None:
    stored = {
        "run_id": "old",
        "started_at": "",
        "probe_ids": [PROBE, "basics.models"],
        "endpoints": {
            TARGET: {
                "role": TARGET,
                "profile_name": "a",
                "base_url": "http://a",
                "model": "m",
                "facts": {
                    PROBE: {"probe": PROBE, "status": "observed", "observed": {"status": 200}, "added_later": 1},
                    "basics.models": {"observed": {}},
                },
            }
        },
    }
    (tmp_path / "run.json").write_text(json.dumps(stored))
    facts = read_run(tmp_path).endpoints[TARGET].facts
    assert facts[PROBE].observed == {"status": 200}
    assert facts["basics.models"].probe == "basics.models"
    assert facts["basics.models"].status == "error"


def test_an_unclaimed_capability_is_asked_when_the_other_side_claims_it(tmp_path: Path) -> None:
    registry.load()
    asked: list[str] = []
    probe = registry.Probe(
        id="test.video_only",
        title="",
        requirement="",
        compare=("x",),
        run=lambda session: asked.append("yes") or {"x": 1},
        requires=("video",),
    )
    profile = Profile(name="text-only", base_url="http://127.0.0.1:9", model="m")
    store = Store(tmp_path, Run(run_id="t", started_at="", probe_ids=[probe.id]))

    run_probes([probe], profile, TARGET, store, verbose=False)
    assert store.run.endpoints[TARGET].facts[probe.id].status == "skipped"

    run_probes([probe], profile, TARGET, store, verbose=False, ask_anyway=("video",))
    fact = store.run.endpoints[TARGET].facts[probe.id]
    assert fact.status == "observed"
    assert asked == ["yes"]
    assert "does not claim video" in fact.notes[0]
