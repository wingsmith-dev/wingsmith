"""Where a run's facts and its evidence live.

One directory per run, one subdirectory per endpoint holding every exchange exactly
as it happened, and one `run.json` at the top that is the only file anything else
reads. `run.json` is written after every probe rather than at the end, so a run
interrupted halfway still has the probes it finished and a report that renders them.

The same reasoning as the benchmark store: a normalised observation nobody can
trace back to bytes is one nobody can defend in front of the person whose
acceptance document this is.
"""

from __future__ import annotations

import json

from typing import Any
from pathlib import Path
from dataclasses import field
from dataclasses import asdict
from dataclasses import fields
from dataclasses import dataclass

from hgrconform.facts import ERROR
from hgrconform.facts import Fact
from hgrconform.profile import Profile


RUN_FILE = "run.json"


@dataclass
class Endpoint:
    """One endpoint's side of a run: which profile, and everything observed of it."""

    role: str
    profile_name: str
    base_url: str
    model: str
    facts: dict[str, Fact] = field(default_factory=dict)

    @classmethod
    def of(cls, role: str, profile: Profile) -> Endpoint:
        return cls(role=role, profile_name=profile.name, base_url=profile.base_url, model=profile.model)


@dataclass
class Run:
    """A whole run: one endpoint, or two when there is a reference to diff against."""

    run_id: str
    started_at: str
    probe_ids: list[str] = field(default_factory=list)
    endpoints: dict[str, Endpoint] = field(default_factory=dict)
    finished_at: str = ""


class Store:
    """The run directory, and the only thing that writes to it."""

    def __init__(self, root: Path, run: Run) -> None:
        self.root = root
        self.run = run
        self.root.mkdir(parents=True, exist_ok=True)

    def evidence_dir(self, role: str) -> Path:
        return self.root / role

    def record(self, role: str, fact: Fact) -> None:
        self.run.endpoints[role].facts[fact.probe] = fact
        self.flush()

    def flush(self) -> None:
        payload: dict[str, Any] = {
            "run_id": self.run.run_id,
            "started_at": self.run.started_at,
            "finished_at": self.run.finished_at,
            "probe_ids": self.run.probe_ids,
            "endpoints": {
                role: {
                    "role": endpoint.role,
                    "profile_name": endpoint.profile_name,
                    "base_url": endpoint.base_url,
                    "model": endpoint.model,
                    "facts": {probe: asdict(fact) for probe, fact in endpoint.facts.items()},
                }
                for role, endpoint in self.run.endpoints.items()
            },
        }
        (self.root / RUN_FILE).write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def read_run(path: Path) -> Run:
    """Load a finished run back off disk, so a comparison can be redone without re-probing."""
    raw = json.loads((path / RUN_FILE).read_text())
    endpoints: dict[str, Endpoint] = {}
    for role, entry in raw["endpoints"].items():
        endpoint = Endpoint(
            role=entry["role"],
            profile_name=entry["profile_name"],
            base_url=entry["base_url"],
            model=entry["model"],
        )
        endpoint.facts = {probe: _fact(probe, fact) for probe, fact in entry["facts"].items()}
        endpoints[role] = endpoint
    return Run(
        run_id=raw["run_id"],
        started_at=raw["started_at"],
        finished_at=raw.get("finished_at", ""),
        probe_ids=raw.get("probe_ids", []),
        endpoints=endpoints,
    )


_FACT_FIELDS = {f.name for f in fields(Fact)}


def _fact(probe: str, raw: dict[str, Any]) -> Fact:
    """A stored fact, read tolerantly: a run written by another version still loads.

    Keys this version does not know are dropped and missing ones take their defaults,
    so a field added or removed between versions does not make every older run
    unreadable. A fact with no status at all is recorded as an error, never as observed.
    """
    known = {key: value for key, value in raw.items() if key in _FACT_FIELDS}
    known.setdefault("probe", probe)
    known.setdefault("status", ERROR)
    return Fact(**known)


def latest_run(results: Path) -> Path | None:
    runs = sorted((path for path in results.glob("*") if (path / RUN_FILE).exists()), key=lambda p: p.name)
    return runs[-1] if runs else None
