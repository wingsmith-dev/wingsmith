"""Where a suite run's results live, and what one measurement records.

One directory per suite run, one subdirectory per cell holding that harness's
artifacts exactly as it wrote them, and one `suite.json` at the top that is the
only file anything else reads. The raw artifacts are kept because a normalised
number nobody can trace back is a number nobody can defend, and because the field
maps in `metrics` are the sort of thing that gets a correction later — when it
does, the fix is to re-read what is already on disk rather than to re-run the
endpoint.

`suite.json` is written after every cell, not at the end. A suite that is
interrupted at rung six of a saturation search still has five rungs on disk and a
report that renders them.
"""

from __future__ import annotations

import os
import json

from typing import Any
from pathlib import Path
from dataclasses import field
from dataclasses import asdict
from dataclasses import dataclass

from hgrbench import metrics
from hgrbench.plan import Cell
from hgrbench.plan import Plan


SUITE_FILE = "suite.json"


@dataclass
class Measurement:
    """One cell, run: what was asked for, what the harness said, what it cost.

    `summary` is the harness's own export, verbatim and unconverted. `readings` is
    what the field map made of it, as `{field id: value}` in the field's declared
    unit, with a separate `missing` list naming the ids the map could not resolve.
    """

    cell_id: str
    stage: str
    harness: str
    harness_version: str
    model: str
    tokenizer: str
    concurrency: int
    request_rate: float
    requests: int
    seconds: int
    input_tokens: int
    output_tokens: int
    repeat: int
    status: str
    started_at: str
    duration_seconds: float
    command: str
    artifacts_dir: str
    readings: dict[str, float] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    derived: dict[str, str] = field(default_factory=dict)
    errors: int = 0
    notes: list[str] = field(default_factory=list)
    summary: Any = None

    @classmethod
    def of(
        cls,
        cell: Cell,
        *,
        status: str,
        started_at: str,
        duration_seconds: float,
        command: str,
        artifacts_dir: str,
        harness_version: str = "unknown",
        summary: Any = None,
        errors: int = 0,
        notes: list[str] | None = None,
    ) -> Measurement:
        """A measurement for one cell, with the field map already applied."""
        readings: dict[str, float] = {}
        missing: list[str] = []
        if summary is not None:
            for field_id, reading in metrics.read(cell.harness, harness_version, summary).items():
                if reading.value is None:
                    missing.append(field_id)
                else:
                    readings[field_id] = reading.value
        return cls(
            cell_id=cell.id,
            stage=cell.stage,
            harness=cell.harness,
            harness_version=harness_version,
            model=cell.model.name,
            tokenizer=cell.model.tokenizer,
            concurrency=cell.load.concurrency,
            request_rate=cell.load.request_rate,
            requests=cell.load.requests,
            seconds=cell.load.seconds,
            input_tokens=cell.input_tokens,
            output_tokens=cell.output_tokens,
            repeat=cell.repeat,
            status=status,
            started_at=started_at,
            duration_seconds=duration_seconds,
            command=command,
            artifacts_dir=artifacts_dir,
            readings=readings,
            missing=missing,
            errors=errors,
            notes=notes or [],
            summary=summary,
        )


@dataclass
class Suite:
    """A whole run of a plan, and everything a report needs to render it."""

    run_id: str
    plan_name: str
    plan_description: str
    plan_source: str
    started_at: str
    endpoint: str
    host: dict[str, str]
    models: list[str]
    harnesses: list[str]
    planned_cells: int
    measurements: list[Measurement] = field(default_factory=list)
    knee: dict[str, Any] = field(default_factory=dict)
    finished_at: str = ""
    notes: list[str] = field(default_factory=list)


class Store:
    """The on-disk home of one suite run."""

    def __init__(self, root: Path, suite: Suite) -> None:
        self.root = root.resolve()
        """Absolute, because these paths become docker bind-mount sources and a
        relative one is read as a named volume rather than as this directory."""
        self.suite = suite
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def suite_path(self) -> Path:
        return self.root / SUITE_FILE

    def cell_dir(self, cell: Cell) -> Path:
        """A private directory for one cell's raw artifacts."""
        path = self.root / "cells" / cell.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def sweep_dir(self, spec: Any) -> Path:
        """A private directory for one ladder run's artifacts.

        Separate from `cells/` because a sweep's output is a tree the harness laid
        out — upstream's own `summary.csv` and per-combination directories — and
        flattening it into the per-cell scheme would destroy the thing that makes
        running upstream's tool worth doing.
        """
        path = self.root / "sweeps" / spec.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def record(self, measurement: Measurement) -> None:
        """Add one measurement and flush the suite file."""
        self.suite.measurements.append(measurement)
        self.flush()

    def flush(self) -> None:
        """Rewrite `suite.json` whole, so a reader never sees half of one.

        Written beside itself and renamed over, which is atomic on one filesystem: an
        interrupt mid-write leaves the previous complete file rather than a truncated
        one that no report can parse.
        """
        payload = json.dumps(asdict(self.suite), indent=2, default=str)
        partial = self.suite_path.with_name(f".{SUITE_FILE}.partial")
        partial.write_text(payload)
        os.replace(partial, self.suite_path)


def read_suite(path: Path) -> dict[str, Any]:
    """One suite run, as the report reads it.

    Returns the plain payload rather than the dataclasses. A report renders a
    suite.json written by an older version of this package, and refusing to read a
    file because a field it does not use has moved would be the wrong trade.
    """
    if path.is_dir():
        path = path / SUITE_FILE
    return json.loads(path.read_text())


def latest_run(root: Path) -> Path | None:
    """The most recent suite directory under a results root."""
    if not root.is_dir():
        return None
    runs = [child for child in root.iterdir() if (child / SUITE_FILE).exists()]
    if not runs:
        return None
    return sorted(runs, key=lambda child: child.name)[-1]


def plan_notes(plan: Plan) -> list[str]:
    """What is true of every measurement in this suite, said once.

    These land in the report's header. They are not decoration: a local runner's
    numbers are wrong in a specific, knowable way, and a report that does not say
    so invites somebody to quote them.
    """
    return [
        "The load generator ran on this machine, so every latency carries the client's own "
        "network path to the endpoint. Compare load points and harnesses against each other; "
        "do not read an absolute latency as the cluster's.",
        "Cells ran one at a time. Two load generators against one deployment measure each other.",
        f"Endpoint: {plan.endpoint.url}",
    ]
