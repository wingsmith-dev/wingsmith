"""What a probe is, and the set of them this runner knows.

A probe is four things: an id, the requirement it answers, the keys on which its
observation is compared against a reference endpoint, and a function that takes a
session and returns a flat dictionary of what it saw. Nothing else — no assertion,
no threshold, no opinion about where results go.

The `requires` field is the only place a probe may decline to run. It names a
capability the profile must claim, so that an endpoint which never promised audio
input is recorded as `skipped` rather than as a failure, and so that a diff against
a reference does not report a difference between two things neither side supports.
"""

from __future__ import annotations

from typing import Any
from pkgutil import iter_modules
from importlib import import_module
from dataclasses import dataclass
from collections.abc import Callable

from hgrconform.session import Session


class Skip(Exception):
    """Raised by a probe that cannot ask its question against this profile.

    A skip is not a failure and not a pass. It is the runner recording that the
    question did not apply — the profile claims no audio input, or declares no
    context window for the overflow probe to overshoot. Comparison treats two skips
    as agreement and a skip against an observation as a difference, which is right:
    if the original provider answers a question ours declines, that is the finding.
    """


@dataclass(frozen=True)
class Probe:
    """One question asked of one endpoint."""

    id: str
    title: str
    requirement: str
    compare: tuple[str, ...]
    run: Callable[[Session], dict[str, Any]]
    requires: tuple[str, ...] = ()
    heavy: bool = False

    @property
    def group(self) -> str:
        return self.id.split(".", 1)[0]


_PROBES: dict[str, Probe] = {}


def register(probe: Probe) -> Probe:
    if probe.id in _PROBES:
        raise ValueError(f"duplicate probe id {probe.id!r}")
    _PROBES[probe.id] = probe
    return probe


def probe(
    id: str,
    title: str,
    requirement: str,
    compare: tuple[str, ...],
    requires: tuple[str, ...] = (),
    heavy: bool = False,
) -> Callable[[Callable[[Session], dict[str, Any]]], Callable[[Session], dict[str, Any]]]:
    """Declare a probe by decorating the function that runs it."""

    def decorate(run: Callable[[Session], dict[str, Any]]) -> Callable[[Session], dict[str, Any]]:
        register(
            Probe(
                id=id,
                title=title,
                requirement=requirement,
                compare=compare,
                run=run,
                requires=requires,
                heavy=heavy,
            )
        )
        return run

    return decorate


def all_probes() -> list[Probe]:
    return [_PROBES[key] for key in sorted(_PROBES)]


def get(id: str) -> Probe:
    if id not in _PROBES:
        known = ", ".join(sorted(_PROBES))
        raise SystemExit(f"no probe {id!r}: one of {known}")
    return _PROBES[id]


def select(only: tuple[str, ...] = (), skip_heavy: bool = True) -> list[Probe]:
    """The probes to run, narrowed by id or by group prefix.

    Heavy probes are out unless asked for by name or unlocked, because one of them
    sends a thousand requests to answer a single row of the standard.
    """
    chosen = all_probes()
    if only:
        chosen = [p for p in chosen if p.id in only or p.group in only or any(p.id.startswith(f"{o}.") for o in only)]
    if skip_heavy:
        chosen = [p for p in chosen if not p.heavy or p.id in only]
    return chosen


_LOADED = False


def load() -> None:
    """Import every module in this package, which is what puts its probes in the registry.

    Discovered rather than listed. A hardcoded import list is one file that every
    new probe has to edit, which makes adding probes in parallel a merge conflict
    and makes forgetting one a probe that silently never runs.
    """
    global _LOADED
    if _LOADED:
        return

    package = import_module(__package__)
    for module in iter_modules(package.__path__):
        if not module.name.startswith("_") and module.name != "registry":
            import_module(f"{__package__}.{module.name}")
    _LOADED = True
