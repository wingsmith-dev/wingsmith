"""Running a set of probes against one or two endpoints.

Serial, and not for the reason the benchmark runner is serial. There, two load
generators against one deployment measure each other; here nothing is being timed,
and the order only matters because some probes intentionally send bad credentials or
oversized prompts, and an endpoint that rate-limits or trips a circuit on those
would poison a concurrent neighbour.

A probe that raises is recorded as an error and the run continues. A probe that
raises `Skip` is recorded as skipped. Neither stops anything, because a run that
stops at the first surprise produces a report with a hole in it and no explanation.
"""

from __future__ import annotations

import time
import traceback

from hgrconform.facts import ERROR
from hgrconform.facts import SKIPPED
from hgrconform.facts import OBSERVED
from hgrconform.facts import Fact
from hgrconform.store import Store
from hgrconform.store import Endpoint
from hgrconform.profile import Profile
from hgrconform.session import Session
from hgrconform.probes.registry import Skip
from hgrconform.probes.registry import Probe


def run_probes(
    probes: list[Probe],
    profile: Profile,
    role: str,
    store: Store,
    verbose: bool = True,
    ask_anyway: tuple[str, ...] = (),
) -> None:
    """Ask every probe of one endpoint, recording each answer before asking the next.

    `ask_anyway` names capabilities to probe for even though this profile does not
    claim them. In a run with a reference it is what the other endpoint claims: an
    endpoint that leaves video out of its profile is still asked about video when the
    reference serves it, so the diff shows what it actually does instead of a skip
    its own profile produced.
    """
    store.run.endpoints[role] = Endpoint.of(role, profile)
    session = Session(profile=profile, evidence_dir=store.evidence_dir(role))

    try:
        for probe in probes:
            fact = _run_one(probe, session, profile, ask_anyway)
            store.record(role, fact)
            if verbose:
                _print(role, fact, probe)
    finally:
        session.close()


def _run_one(probe: Probe, session: Session, profile: Profile, ask_anyway: tuple[str, ...] = ()) -> Fact:
    unclaimed = [capability for capability in probe.requires if not profile.supports(capability)]
    missing = [capability for capability in unclaimed if capability not in ask_anyway]
    if missing:
        return Fact(
            probe=probe.id,
            status=SKIPPED,
            notes=[f"profile does not claim {', '.join(missing)}"],
        )
    notes = [f"asked although the profile does not claim {', '.join(unclaimed)}"] if unclaimed else []

    before = len(session.exchanges)
    started = time.monotonic()
    try:
        observed = probe.run(session)
        status, error = OBSERVED, ""
    except Skip as exc:
        observed, status, error = {}, SKIPPED, str(exc)
    except Exception as exc:
        observed, status = {}, ERROR
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"

    return Fact(
        probe=probe.id,
        status=status,
        observed=observed,
        exchanges=session.exchanges[before:],
        duration_seconds=round(time.monotonic() - started, 3),
        error=error,
        notes=notes + ([error] if status == SKIPPED and error else []),
    )


MARKS = {OBSERVED: "  ok", SKIPPED: "skip", ERROR: " err"}


def _print(role: str, fact: Fact, probe: Probe) -> None:
    mark = MARKS.get(fact.status, "    ")
    print(f"  {mark}  {role:<9} {probe.id:<28} {fact.duration_seconds:>6.2f}s")
    if fact.status == ERROR:
        print(f"        {fact.error.splitlines()[0]}")
