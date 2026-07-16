"""Measure, once per fleet, the thresholds every later run is judged against.

This is the fixed cost that makes a method transfer. Nothing downstream holds a number:
the gate asks a deployment profile, and a deployment profile is only ever produced here,
from runs that actually happened on the fleet in question.

Four things come out of it, and each one replaces a literal that has gone stale in this
project at least once:

**Capacity** is read, never derived. The engine publishes the size of its own KV pool, so
this is free and needs no benchmark at all -- and it is right across attention geometries
that a derivation would have to special-case, since a pool replicated across
tensor-parallel ranks and one sharded across them are already reconciled by the time the
engine reports a number.

**The healthy band** for prefix hit rate is a property of a shape on a fleet, not a
constant. The same deployment under single-turn traffic has no reuse at all, and a band
copied between shapes reports a working deployment as broken.

**The noise floor** is the difference below which this instrument cannot tell two
configurations apart. It must come from repeats that cross a reload, because nearly every
comparison worth making does: a flag change costs a worker reload, and the variance
between two loads of the same configuration -- node placement, cache warmth, fabric path
-- is strictly larger than the variance within one. Repeats taken back to back on one
generation give a floor that is too small, and a floor that is too small turns noise into
findings. That is recorded rather than hidden: a profile calibrated without crossing a
reload says so, and says the floor is a lower bound.

**The standard cell** is the smallest run whose answer matches the big one. Establish it
once and every later question costs that instead of the full shape.
"""

from __future__ import annotations

import statistics

from typing import Sequence

from hgrbench.fleet import adapters
from hgrbench.gate import Run
from hgrbench.profile import Calibration
from hgrbench.profile import DeploymentProfile
from hgrbench.profile import WorkloadProfile


# A repeat set's standard deviation is doubled to approximate a two-sided band. With the
# handful of repeats anybody actually fires this is a coarse instrument, which is the
# point: it is honest about being coarse rather than precise about being wrong.
COVERAGE = 2.0


def capacity(run: Run, window: tuple[int, int]) -> dict[str, Calibration]:
    """Per-role KV pool, as the engine reports it, over the run's own window."""
    out: dict[str, Calibration] = {}
    d = run.dump
    if d is None:
        return out
    lo, hi = window
    for role in d.roles_in(lo, hi):
        if role in ("idle", "absent"):
            continue
        tokens, why = d.capacity_tokens(role, lo, hi)
        if tokens is None:
            continue
        out[role] = Calibration(
            value=tokens, unit="tokens", runs=[run.label],
            note=f"read from the engine's own cache config on "
                 f"{len(d.roles_in(lo, hi)[role])} worker(s)")
    return out


def hit_band(runs: Sequence[tuple[Run, tuple[int, int]]],
             margin: float = 0.05) -> dict[str, list[float]]:
    """The prefix hit rate a healthy run of this shape holds, per role.

    Taken from runs already judged valid. A band derived from a run that was itself
    degrading would enshrine the degradation as normal, which is how a threshold quietly
    stops catching the thing it was written for.
    """
    seen: dict[str, list[float]] = {}
    for run, (lo, hi) in runs:
        if run.dump is None:
            continue
        for role in run.dump.roles_in(lo, hi):
            if role in ("idle", "absent"):
                continue
            s = run.dump.summarise(lo, hi, role)
            if s is None:
                continue
            hit = s.ratio(adapters.CACHE_HITS, adapters.CACHE_QUERIES)
            if hit is not None:
                seen.setdefault(role, []).append(hit)
    return {role: [max(0.0, min(v) - margin), min(1.0, max(v) + margin)]
            for role, v in seen.items() if v}


def noise_floor(runs: Sequence[Run], metric: str = "request_throughput",
                crossed_reload: bool = False) -> Calibration | None:
    """The smallest difference this instrument can resolve, from repeats.

    Repeats, not arms: every run here must be the same serving configuration. Two runs
    give a difference and not a variance, so a floor from n=2 is reported with its n and
    should be widened as repeats accumulate.
    """
    vals = [(r.label, r.num(metric, "avg")) for r in runs]
    vals = [(lbl, v) for lbl, v in vals if v]
    if len(vals) < 2:
        return None
    xs = [v for _, v in vals]
    mean = statistics.fmean(xs)
    if mean <= 0:
        return None
    spread = (max(xs) - min(xs)) / mean
    cv = (statistics.stdev(xs) / mean) if len(xs) > 2 else spread
    floor = max(COVERAGE * cv, spread)
    note = (f"n={len(xs)} repeats of one configuration, "
            f"{'across a reload' if crossed_reload else 'within one worker generation -- '
               'a LOWER BOUND, since a reload adds variance this never saw'}")
    return Calibration(value=floor, unit="fraction", runs=[lbl for lbl, _ in vals], note=note)


def standard_cell(small: Run, large: Run, floor: float,
                  metric: str = "request_throughput") -> dict[str, int] | None:
    """The cheaper cell, if its answer matches the expensive one inside the floor.

    Run length is bought with session count, never with session depth. Depth decides how
    far a session's context is amortised, so shortening it changes the workload; session
    count barely moves the answer because the working set is concurrency times context,
    not sessions times context.
    """
    a, b = small.num(metric, "avg"), large.num(metric, "avg")
    if not a or not b:
        return None
    if abs(a - b) / b >= floor:
        return None
    import re
    m = re.search(r"--num-conversations (\d+)", small.invocation)
    n = re.search(r"--request-count (\d+)", small.invocation)
    if not m or not n:
        return None
    return {"sessions": int(m.group(1)), "requests": int(n.group(1))}


def build(model: str, topology: str, engine: str, workload: WorkloadProfile,
          fingerprint: dict, baseline: Run, window: tuple[int, int],
          repeats: Sequence[Run] = (), crossed_reload: bool = False,
          healthy: Sequence[tuple[Run, tuple[int, int]]] = ()) -> DeploymentProfile:
    p = DeploymentProfile(
        model=model, topology=topology, engine=engine, workload=workload.name,
        fingerprint=fingerprint,
        capacity_tokens=capacity(baseline, window),
        hit_rate_band=hit_band(healthy or [(baseline, window)]),
        noise_floor=noise_floor(repeats or [baseline], crossed_reload=crossed_reload),
        baseline={k: v for k, v in (
            ("request_throughput", baseline.num("request_throughput", "avg") or 0.0),
            ("ttft_p90_ms", baseline.num("time_to_first_token", "p90") or 0.0),
        )},
    )
    return p
