"""A directory of Prometheus scrapes, read as the fleet that served one run.

A sampler writes `<elapsed-seconds>__<pod>.prom` on an interval for the length of a
benchmark, or for longer -- samplers leak, and a leaked one keeps scraping into the next
run, and the next fleet. So a dump is not a run, and the single most expensive mistake
this module exists to prevent is summarising the wrong one. Of the telemetry dumps this
was written against, ten of seventeen hold more than one benchmark and the largest holds
eleven across five worker generations.

Three things follow, and they are the whole design:

**The window is pinned to the run, never inferred from the dump.** Scrape files carry
their wall-clock time in their mtime and the load generator records `start_time` and
`end_time`, so the intersection is exact. Everything else -- matching a segment's
completion count against the run's request count, and finally the longest active segment
-- is a labelled fallback, and which one was used is part of the result. Choosing the
longest segment silently, which is the obvious rule, returns a different benchmark's
numbers on four of the five multi-run dumps here.

**Deltas are per pod, then summed.** Differencing a sum over pods goes negative whenever
one scrape fails, and taking one pod's delta as the role's -- which is what happens if a
loop variable outlives its loop -- under-reports a two-pod role by about half and a
three-pod role by about two thirds. The rate denominator is each pod's own span, because
pods do not start and stop together.

**Per-pod figures survive into the summary.** Two workers in one role are assumed to
share load and sometimes do not; a role total hides it completely. One run here spent its
second half with a single prefill worker holding a queue of fifty while its peer sat idle
at six percent, and every role-level number looked like a cache problem.
"""

from __future__ import annotations

import os
import re
import glob
import math
import statistics

from typing import Any
from pathlib import Path
from datetime import timezone
from datetime import datetime
from dataclasses import field
from dataclasses import dataclass

from hgrbench.fleet import adapters
from hgrbench.fleet.adapters import Engine


SAMPLE = re.compile(r"^([a-z_]+:[a-z0-9_]+)(\{(.*)\})?\s+([0-9.eE+-]+)\s*$")
LABEL = re.compile(r'([a-z_0-9]+)="([^"]*)"')
FILENAME = re.compile(r"^(\d+)__(.+?)(?:\.prom)?$")

PREFILL_ONLY_TOKENS_PER_REQUEST = 2.0


@dataclass(frozen=True)
class Sample:
    """One pod's scrape at one instant: counters and gauges summed over label sets.

    Label sets are collapsed on purpose. A worker publishes the same counter split by
    finish reason, by model, by adapter; the split is never the question here and
    keeping it would make every reader re-sum it. `info` is kept unsummed because an
    info metric's value is in its labels.
    """

    t: int
    wall: float
    values: dict[str, float]
    info: dict[str, str]


@dataclass
class Pod:
    """One worker across the dump, and what it turned out to be doing."""

    name: str
    samples: dict[int, Sample] = field(default_factory=dict)
    generation: str = ""

    def at(self, t: int) -> Sample | None:
        return self.samples.get(t)

    def span(self, lo: int, hi: int) -> tuple[Sample, Sample] | None:
        ts = sorted(t for t in self.samples if lo <= t <= hi)
        if len(ts) < 2:
            return None
        return self.samples[ts[0]], self.samples[ts[-1]]


@dataclass(frozen=True)
class Window:
    """A stretch of a dump, and how honestly it was chosen.

    `basis` is load-bearing. "clock" means the load generator's own start and end times
    were intersected with the scrape times and the window is the run. "requests" means
    the run's completion count picked the segment out. "longest" means neither was
    available and this is a guess -- a caller reporting a number from a "longest" window
    is reporting a number about an unidentified benchmark, and should say so.
    """

    lo: int
    hi: int
    basis: str
    segments: int
    note: str = ""

    @property
    def trustworthy(self) -> bool:
        return self.basis in ("clock", "requests")


@dataclass
class RoleSummary:
    """One role over one window: totals, per-pod detail, and what could not be read."""

    role: str
    pods: list[str]
    seconds: float
    totals: dict[str, float]
    per_pod: dict[str, dict[str, float]]
    missing: list[str]
    dropped: dict[str, str]

    def rate(self, series_id: str) -> float | None:
        """Completions per second, summed as per-pod rates.

        Not total delta over one pod's span: pods rarely cover identical stretches, and
        dividing a summed numerator by one pod's denominator understates a role whose
        pods started at different times.
        """
        v = self.totals.get(f"{series_id}.rate")
        return v

    def ratio(self, num: str, den: str) -> float | None:
        n, d = self.totals.get(num), self.totals.get(den)
        if n is None or d is None or d <= 0:
            return None
        return n / d

    def mean(self, stem: str) -> float | None:
        return self.ratio(f"{stem}.sum", f"{stem}.count")

    def balance(self, series_id: str) -> float | None:
        """Least-loaded pod's share of the busiest pod's, over the window.

        1.0 is even. This is the figure that separates "the fleet is saturated" from
        "one worker is doing the work", and no role total can show it.
        """
        vals = [p.get(series_id, 0.0) for p in self.per_pod.values()]
        vals = [v for v in vals if v is not None]
        if len(vals) < 2 or max(vals) <= 0:
            return None
        return min(vals) / max(vals)


class Dump:
    """Every scrape in one directory, indexed by pod and time."""

    def __init__(self, path: str | Path, engine: Engine | None = None):
        self.path = Path(path)
        self.pods: dict[str, Pod] = {}
        self.engine = engine
        self.metric_names: set[str] = set()
        self._sampler_start: float | None = None
        self._load()

    def _load(self) -> None:
        files = glob.glob(str(self.path / "*.prom"))
        if not files:
            raise FileNotFoundError(f"no scrapes in {self.path}")

        raw: list[tuple[int, str, str, float]] = []
        for f in files:
            m = FILENAME.match(os.path.basename(f))
            if not m:
                continue
            raw.append((int(m.group(1)), m.group(2), f, os.path.getmtime(f)))

        # Elapsed seconds are relative to whenever the sampler started, which nothing
        # records. The mtimes do record it, so the offset is recoverable: take the
        # median of (mtime - elapsed) rather than the minimum, because a slow scrape
        # shifts one file and a median ignores it.
        if raw:
            self._sampler_start = statistics.median(mt - t for t, _, _, mt in raw)

        for t, pod, f, _ in raw:
            values, info, names = self._parse(f)
            self.metric_names |= names
            if pod not in self.pods:
                gen = ""
                g = re.search(r"-([0-9a-f]{8})-", pod)
                if g:
                    gen = g.group(1)
                self.pods[pod] = Pod(name=pod, generation=gen)
            wall = (self._sampler_start or 0.0) + t
            self.pods[pod].samples[t] = Sample(t=t, wall=wall, values=values, info=info)

        if self.engine is None:
            self.engine = adapters.detect(self.metric_names)

    def _parse(self, path: str) -> tuple[dict[str, float], dict[str, str], set[str]]:
        values: dict[str, float] = {}
        info: dict[str, str] = {}
        names: set[str] = set()
        with open(path) as fh:
            for line in fh:
                if not line or line[0] == "#":
                    continue
                m = SAMPLE.match(line.rstrip("\n"))
                if not m:
                    continue
                name, labels, val = m.group(1), m.group(3) or "", float(m.group(4))
                names.add(name)
                values[name] = values.get(name, 0.0) + val
                if labels and name.endswith("_info"):
                    for k, v in LABEL.findall(labels):
                        info.setdefault(f"{name}/{k}", v)
        return values, info, names

    def read(self, sample: Sample, series_id: str) -> float | None:
        """One series from one scrape, through the engine's map. None is a named miss."""
        if self.engine is None:
            return None
        for s in self.engine.series:
            if s.id != series_id:
                continue
            if s.kind == adapters.INFO:
                raw = sample.info.get(f"{s.metric}/{s.label}")
                if raw is None:
                    return None
                try:
                    return float(raw) * s.scale
                except ValueError:
                    return None
            v = sample.values.get(s.metric)
            return None if v is None else v * s.scale
        return None

    def roles_in(self, lo: int, hi: int) -> dict[str, list[str]]:
        """Each pod's role over one window, inferred from what it did there.

        Scoped to the window rather than to the dump, because a dump routinely spans
        several fleet generations and a pod that served in some other run contributes
        nothing here but would still be counted into a role. One dump in this corpus
        holds thirty-five workers across eight generations; read whole, its two-prefill
        run reads as one prefill and three decodes.

        Behaviour, not naming. Pod names are a property of whichever operator rendered
        the deployment, and this has to read a fleet rendered by something else next
        year. A worker serving only prefill emits about one generated token per
        completed request -- the first one -- while a worker that decodes emits the
        whole output length. A fleet where every worker decodes is aggregated, however
        its pods are named, which is what makes an aggregated Dynamo deployment
        readable at all: it renders its workers as `...decodeworker`, and any rule that
        trusted the name would call it disaggregated.
        """
        prefill_like: list[str] = []
        decode_like: list[str] = []
        out: dict[str, list[str]] = {}
        for name, pod in self.pods.items():
            got = pod.span(lo, hi)
            if got is None:
                out.setdefault("absent", []).append(name)
                continue
            a, b = got
            done = (self.read(b, adapters.REQUESTS_DONE) or 0) - (
                self.read(a, adapters.REQUESTS_DONE) or 0)
            gen = (self.read(b, adapters.TOKENS_GENERATED) or 0) - (
                self.read(a, adapters.TOKENS_GENERATED) or 0)
            if done <= 0:
                out.setdefault("idle", []).append(name)
                continue
            (prefill_like if gen / done <= PREFILL_ONLY_TOKENS_PER_REQUEST
             else decode_like).append(name)

        for name in prefill_like:
            out.setdefault("prefill", []).append(name)
        serving = "decode" if prefill_like else "aggregated"
        for name in decode_like:
            out.setdefault(serving, []).append(name)
        return out

    def generations_in(self, lo: int, hi: int) -> set[str]:
        """Worker generations that published inside the window.

        More than one means the fleet rolled mid-run, and the two halves of the window
        are two different serving configurations.
        """
        out = set()
        for pod in self.pods.values():
            if pod.span(lo, hi) and pod.generation:
                out.add(pod.generation)
        return out

    @property
    def times(self) -> list[int]:
        return sorted({t for p in self.pods.values() for t in p.samples})

    # ── choosing a window ──────────────────────────────────────────────────────

    def completion_series(self) -> list[tuple[int, int, float]]:
        """(from, to, completions per second) for every consecutive pair of scrapes."""
        ts = self.times
        out = []
        for a, b in zip(ts, ts[1:]):
            dt = b - a
            if dt <= 0:
                continue
            total = 0.0
            for pod in self.pods.values():
                sa, sb = pod.at(a), pod.at(b)
                if sa is None or sb is None:
                    continue
                va = self.read(sa, adapters.REQUESTS_DONE)
                vb = self.read(sb, adapters.REQUESTS_DONE)
                if va is None or vb is None:
                    continue
                total += max(vb - va, 0.0)
            out.append((a, b, total / dt))
        return out

    def segments(self, idle_rate: float = 0.0, gap: int = 2) -> list[tuple[int, int, float]]:
        """Stretches where the fleet was serving, as (from, to, completions).

        `idle_rate` defaults to zero -- strictly, any completion at all counts as
        serving. Thresholding on a fraction of peak looks tidier and is wrong in the
        one case that matters: a run degrading from sixteen requests per second to two
        is still serving, and clipping it removes exactly the failure worth seeing.
        Idle between runs is genuinely zero, so zero separates them.
        """
        segs: list[tuple[int, int, float]] = []
        cur: list[int] | None = None
        done = 0.0
        quiet = 0
        for a, b, rate in self.completion_series():
            if rate > idle_rate:
                if cur is None:
                    cur = [a, b]
                    done = 0.0
                cur[1] = b
                done += rate * (b - a)
                quiet = 0
            elif cur is not None:
                quiet += 1
                if quiet >= gap:
                    segs.append((cur[0], cur[1], done))
                    cur, quiet = None, 0
        if cur is not None:
            segs.append((cur[0], cur[1], done))
        return segs

    def window(self, start: str | None = None, end: str | None = None,
               request_count: float | None = None, roles_counting: int = 1) -> Window:
        """The stretch of this dump that is the run, and how that was decided."""
        segs = self.segments()
        n = len(segs)
        if not segs:
            ts = self.times
            return Window(ts[0], ts[-1], "empty", 0, "no completions anywhere in the dump")

        if start and end and self._sampler_start is not None:
            lo = self._to_elapsed(start)
            hi = self._to_elapsed(end)
            if lo is not None and hi is not None:
                ts = [t for t in self.times if lo <= t <= hi]
                if len(ts) >= 2:
                    covered = (ts[-1] - ts[0]) / max(hi - lo, 1)
                    note = "" if covered > 0.9 else (
                        f"scrapes cover {100 * covered:.0f}% of the run; the sampler "
                        f"started late or stopped early")
                    return Window(ts[0], ts[-1], "clock", n, note)

        if request_count:
            want = request_count * max(roles_counting, 1)
            best = min(segs, key=lambda s: abs(s[2] - want))
            if want > 0 and abs(best[2] - want) / want < 0.1:
                return Window(best[0], best[1], "requests", n,
                              f"segment completions {best[2]:.0f} match the run's "
                              f"{want:.0f}")

        longest = max(segs, key=lambda s: s[1] - s[0])
        return Window(longest[0], longest[1], "longest", n,
                      f"no run identity available; picked the longest of {n} serving "
                      f"period(s), which is not necessarily this run")

    def _to_elapsed(self, iso: str) -> int | None:
        if self._sampler_start is None:
            return None
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() - self._sampler_start)

    # ── reading a window ───────────────────────────────────────────────────────

    def summarise(self, lo: int, hi: int, role: str) -> RoleSummary | None:
        """One role over one window, per-pod deltas summed and per-pod detail kept."""
        names = self.roles_in(lo, hi).get(role, [])
        pods = [self.pods[n] for n in names]
        if not pods:
            return None
        counters = [s.id for s in (self.engine.series if self.engine else ())
                    if s.kind == adapters.COUNTER]
        gauges = [s.id for s in (self.engine.series if self.engine else ())
                  if s.kind == adapters.GAUGE]

        totals: dict[str, float] = {}
        per_pod: dict[str, dict[str, float]] = {}
        dropped: dict[str, str] = {}
        missing: set[str] = set()
        seconds = 0.0

        for pod in pods:
            got = pod.span(lo, hi)
            if got is None:
                dropped[pod.name] = "fewer than two scrapes in the window"
                continue
            a, b = got
            dt = b.t - a.t
            if dt <= 0:
                dropped[pod.name] = "zero-length span"
                continue
            reset = False
            deltas: dict[str, float] = {}
            for cid in counters:
                va, vb = self.read(a, cid), self.read(b, cid)
                if va is None or vb is None:
                    missing.add(cid)
                    continue
                if vb < va:
                    reset = True
                    break
                deltas[cid] = vb - va
            if reset:
                # A counter that goes backwards is a restarted worker, not a negative
                # rate. Its window is two different processes and no delta across it
                # means anything, so the pod leaves the summary by name.
                dropped[pod.name] = "counter reset; the worker restarted mid-window"
                continue
            for gid in gauges:
                vb = self.read(b, gid)
                if vb is None:
                    missing.add(gid)
                    continue
                deltas[f"{gid}.last"] = vb
            per_pod[pod.name] = deltas
            seconds = max(seconds, float(dt))
            for k, v in deltas.items():
                totals[k] = totals.get(k, 0.0) + v
            for cid, v in deltas.items():
                if cid in counters:
                    totals[f"{cid}.rate"] = totals.get(f"{cid}.rate", 0.0) + v / dt

        if not per_pod:
            return RoleSummary(role, [p.name for p in pods], 0.0, {}, {},
                               sorted(missing), dropped)
        for oid in adapters.OPTIONAL:
            missing.discard(oid)
        return RoleSummary(role, sorted(per_pod), seconds, totals, per_pod,
                           sorted(missing), dropped)

    def capacity_tokens(self, role: str, lo: int, hi: int) -> tuple[float | None, str]:
        """The role's KV pool, as the engine itself reports it, summed over workers.

        Read, never derived. This is the number that decides whether a workload fits,
        and deriving it needs the attention geometry, the quantisation, the memory
        fraction and the activation footprint -- four things that change per model and
        per build, and one calibration study here found NVIDIA's model of them off by
        18 to 40 percent on this fleet.
        """
        names = self.roles_in(lo, hi).get(role, [])
        if not names:
            return None, f"no {role} workers served inside this window"
        total = 0.0
        for pod in (self.pods[n] for n in names):
            ts = sorted(t for t in pod.samples if lo <= t <= hi)
            if not ts:
                continue
            v = self.read(pod.samples[ts[-1]], adapters.KV_CAPACITY_TOKENS)
            if v is None:
                return None, (f"{self.engine.engine if self.engine else 'engine'} "
                              f"published no KV capacity on {pod.name}")
            total += v
        return total, ""
