"""What one probe observed, and the evidence that it observed it.

A fact carries no verdict. `observed` is a flat dictionary of small comparable
values (a status code, a field path, a boolean, a count) and that flatness is
essential: comparison against a reference endpoint is a key-by-key diff of two
of these, so anything nested is something the diff cannot explain.

The reason a probe does not decide pass or fail is that almost every question
asked of an endpoint serving someone else's model is phrased as *consistent with
the original model provider*. There is no right answer to assert against, only another endpoint to
agree with. A probe that asserted would have to be rewritten for each model; one
that observes does not.
"""

from __future__ import annotations

from typing import Any
from dataclasses import field
from dataclasses import dataclass


OBSERVED = "observed"
ERROR = "error"
SKIPPED = "skipped"


@dataclass
class Fact:
    """One probe, run once, against one endpoint."""

    probe: str
    status: str
    observed: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    exchanges: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    error: str = ""

    @property
    def usable(self) -> bool:
        return self.status == OBSERVED


@dataclass
class Difference:
    """One key on which two endpoints did not agree."""

    probe: str
    key: str
    target: Any
    reference: Any
    kind: str

    @property
    def label(self) -> str:
        return f"{self.probe}.{self.key}" if self.key else self.probe


class _Absent:
    def __repr__(self) -> str:
        return "<absent>"


_ABSENT = _Absent()


def compare(target: Fact, reference: Fact, keys: tuple[str, ...]) -> list[Difference]:
    """Diff one probe's two runs on the keys that probe declared comparable.

    A key present on one side only is a difference, not a gap, because that is
    exactly the shape of the failures this is looking for: a field the original
    provider returns and we do not.
    """
    if not (target.usable and reference.usable):
        return [
            Difference(
                probe=target.probe,
                key="",
                target=target.status,
                reference=reference.status,
                kind="not-run",
            )
        ]

    differences: list[Difference] = []
    for key in keys:
        here = target.observed.get(key, _ABSENT)
        there = reference.observed.get(key, _ABSENT)
        if here == there:
            continue
        kind = "missing" if here is _ABSENT or there is _ABSENT else "differs"
        differences.append(
            Difference(
                probe=target.probe,
                key=key,
                target=None if here is _ABSENT else here,
                reference=None if there is _ABSENT else there,
                kind=kind,
            )
        )
    return differences
