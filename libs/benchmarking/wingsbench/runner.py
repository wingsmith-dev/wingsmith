"""Running a plan: one cell at a time, and the saturation search that follows it.

Everything here is serial and that is the design, not a limitation waiting to be
lifted. Two load generators pointed at one deployment measure each other, and a
matrix is only useful if its cells are comparable, so they run back to
back, with a cooldown between them, and the report can say a difference came from
the harness, not from what else was running.

The saturation search is the one stage that can hurt a live endpoint, so it is
opt-in twice: `[knee] enabled` in the plan, and `--hard` on the invocation. It
climbs a ladder, stops at the first rung that breaks the SLA or produces errors,
and reports the last rung that held. Stopping *at* the breach instead of after it
means the endpoint spends one load point in trouble, not the rest of the ladder.
"""

from __future__ import annotations

import os
import time
import threading
import subprocess

from typing import TextIO
from urllib import error as urlerror
from urllib import request as urlrequest
from pathlib import Path
from datetime import UTC
from datetime import datetime
from dataclasses import replace
from dataclasses import dataclass

from wingsbench import metrics
from wingsbench.plan import Cell
from wingsbench.plan import Load
from wingsbench.plan import Plan
from wingsbench.plan import expand
from wingsbench.plan import knee_cells
from wingsbench.plan import sweep_specs
from wingsbench.store import Store
from wingsbench.store import Measurement
from wingsbench.harnesses import registry
from wingsbench.harnesses.base import Context
from wingsbench.harnesses.base import Harness
from wingsbench.harnesses.base import Emission
from wingsbench.harnesses.base import Invocation
from wingsbench.harnesses.base import HarnessError
from wingsbench.harnesses.base import pull
from wingsbench.harnesses.base import scrub
from wingsbench.harnesses.base import host_arch
from wingsbench.harnesses.base import image_exists
from wingsbench.harnesses.base import resolve_digest


DEFAULT_TIMEOUT = 1800


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Options:
    """How this invocation differs from the plan as written."""

    hard: bool = False
    dry_run: bool = False
    only_harness: tuple[str, ...] = ()
    only_model: tuple[str, ...] = ()
    timeout: int = DEFAULT_TIMEOUT
    keep_going: bool = True
    hf_cache: Path = Path.home() / ".cache" / "huggingface"
    quiet: bool = False


class Runner:
    """Drives one plan against one endpoint, writing into one store."""

    def __init__(self, plan: Plan, store: Store, options: Options) -> None:
        self.plan = plan
        self.store = store
        self.options = options
        self.token = ""
        self.images: dict[str, str] = {}
        self.arch = host_arch()

    # ── setup ────────────────────────────────────────────────────────────────

    def resolve_token(self) -> str:
        """The bearer token, read once from the variable the plan names.

        Once per suite, not per cell, so a variable changed mid-run cannot
        give two cells of one suite two different identities.
        """
        return os.environ.get(self.plan.endpoint.token_env, "")

    def probe(self) -> None:
        """One request to the endpoint before spending an hour on it.

        `/v1/models` instead of `/health`: this gateway answers 400 there with a
        token and 401 without one, and a benchmark suite that dies forty minutes in
        because the port-forward was never up is the failure this prevents.
        """
        url = self.plan.endpoint.probe_url.rstrip("/") + "/v1/models"
        req = urlrequest.Request(url)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urlrequest.urlopen(req, timeout=15) as response:
                if response.status != 200:
                    raise RuntimeError(f"{url} answered {response.status}")
        except urlerror.HTTPError as exc:
            raise RuntimeError(
                f"{url} answered {exc.code}. "
                "A 401 means the token is wrong or absent; a 404 means this is not the gateway."
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"{url} is not reachable from this host ({exc}). "
                "If the endpoint is a port-forward, it has to be up before the suite starts."
            ) from exc
        self.say(f"endpoint answers at {url}")

    def prepare_images(self, harnesses: list[str]) -> None:
        """Resolve every image the run needs, before the first cell.

        A registry image is pinned to the digest of this architecture's variant and
        pulled. A locally built one is built if it is missing, because a harness we
        build ourselves has no registry to fall back on and discovering that at cell
        one is a waste of the setup that came before it.
        """
        for name in harnesses:
            harness = registry.get(name)
            if harness.build_context:
                if not image_exists(harness.image):
                    self.build(harness)
                self.images[name] = harness.image
                continue
            pinned = resolve_digest(harness.reference, self.arch)
            if not image_exists(pinned):
                self.say(f"pulling {harness.image} for linux/{self.arch}")
                pull(pinned)
            self.images[name] = pinned

    def build(self, harness: Harness) -> None:
        """Build a harness image from the context committed beside it."""
        context = Path(harness.build_context)
        if not context.is_dir():
            raise HarnessError(
                f"{harness.name} needs the image {harness.image}, which is built from "
                f"{harness.build_context}; that directory ships with the source tree, not the "
                f"package, so build it from a checkout or load the image some other way"
            )
        self.say(f"building {harness.image} from {harness.build_context}")
        done = subprocess.run(["docker", "build", "-t", harness.image, str(context)], text=True)
        if done.returncode != 0:
            raise HarnessError(f"docker build for {harness.name} failed")

    # ── running ──────────────────────────────────────────────────────────────

    def context_for(self, cell: Cell) -> Context:
        return Context(
            endpoint_url=self.plan.endpoint.url,
            token=self.token,
            hf_cache=self.options.hf_cache,
            results_dir=self.store.cell_dir(cell),
            image=self.images[cell.harness],
        )

    def run_cell(self, cell: Cell) -> list[Measurement]:
        """One measurement, start to finish, recorded whatever happens.

        A failed cell is recorded as failed, not dropped. A suite whose
        results are only the cells that worked is a suite that quietly reports the
        load levels the endpoint could handle and forgets the ones it could not,
        which is the exact opposite of what a saturation test is for.
        """
        harness = registry.get(cell.harness)
        ctx = self.context_for(cell)
        invocation = harness.command(cell, ctx)
        started = now()
        log = ctx.results_dir / "harness.log"

        if self.options.dry_run:
            self.say(f"  would run: {invocation.display}")
            return []

        returncode, elapsed = self.execute(invocation, log, self.cell_timeout(cell))
        status = "ok" if returncode == 0 else f"exit {returncode}"
        notes: list[str] = []
        try:
            emissions = harness.read(cell, ctx)
        except (HarnessError, ValueError) as exc:
            self.say(f"  {cell.harness}: {exc}")
            measurement = Measurement.of(
                cell,
                status="unreadable" if returncode == 0 else status,
                started_at=started,
                duration_seconds=round(elapsed, 3),
                command=invocation.display,
                artifacts_dir=str(ctx.results_dir),
                notes=[str(exc)],
            )
            self.store.record(measurement)
            return [measurement]

        measurements: list[Measurement] = []
        taken: set[str] = set()
        for emission in emissions:
            point = cell if len(emissions) == 1 else self.relabel(cell, emission, taken)
            taken.add(point.id)
            measurement = Measurement.of(
                point,
                status=status,
                started_at=started,
                duration_seconds=round(elapsed, 3),
                command=invocation.display,
                artifacts_dir=str(ctx.results_dir),
                harness_version=emission.version,
                summary=emission.summary,
                notes=notes + emission.notes,
            )
            self.apply_derived(measurement, emission.summary)
            measurement.errors = int(measurement.readings.get(metrics.ERROR_COUNT, 0))
            self.grade(measurement)
            if emission.load_label:
                measurement.notes.append(f"harness reported this point as {emission.load_label}")
            self.store.record(measurement)
            measurements.append(measurement)
            self.report_cell(measurement)
        return measurements

    def execute(self, invocation: Invocation, log: Path, timeout: int) -> tuple[int, float]:
        """Run one container, writing a log the token cannot leak through as it goes.

        Each line is redacted on its way to the file instead of streamed straight
        there, because one of these harnesses echoes the whole command it spawns to
        its own stdout, `--header "Authorization=Bearer <token>"` and all. A log
        is a file somebody pastes into an issue.

        A run that outlives `timeout` has its container killed by name, not just the
        docker CLI that started it: killing the CLI leaves the load generator running
        against the endpoint, into the next cell. What it wrote before then stays in
        the log, which is the only account of why it hung.
        """
        clock = time.monotonic()
        with log.open("w") as sink:
            sink.write(invocation.display + "\n\n")
            sink.flush()
            process = subprocess.Popen(
                invocation.argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env={**os.environ, **invocation.env} if invocation.env else None,
            )
            reader = threading.Thread(target=self._drain, args=(process, sink), daemon=True)
            reader.start()
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.kill(invocation, process)
                reader.join(timeout=30)
                sink.write(f"\nwingsbench: timed out after {timeout}s; the container was killed\n")
                raise
            reader.join()
        return returncode, time.monotonic() - clock

    def _drain(self, process: subprocess.Popen[str], sink: TextIO) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            sink.write(self.redact(line))
            sink.flush()

    def redact(self, text: str) -> str:
        return scrub(text, self.token)

    def kill(self, invocation: Invocation, process: subprocess.Popen[str]) -> None:
        """Stop an overdue run: the container first, then the CLI that was waiting on it."""
        if invocation.container:
            subprocess.run(["docker", "kill", invocation.container], capture_output=True, text=True)
        process.kill()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.say(f"  the docker CLI for {invocation.container or 'this run'} did not exit after a kill")

    def grade(self, measurement: Measurement) -> None:
        """Correct a status that the harness's exit code got wrong.

        `vllm bench serve` exits 0 when every single request failed: it returns its
        result dict either way. So a cell is `ok` only if something was measured, and
        the request count in the export is what says so.
        """
        completed = measurement.readings.get(metrics.REQUEST_COUNT)
        if measurement.status != "ok" or completed is None:
            return
        if completed <= 0:
            measurement.status = "no requests completed"
            measurement.notes.append(
                "the harness exited cleanly and measured nothing; its own export reports no completed requests"
            )

    def relabel(self, cell: Cell, emission: Emission, taken: set[str]) -> Cell:
        """A cell id for one point of a multi-point run.

        A harness that walks a ladder in one process produced several measurements
        from one cell, and they need distinct ids or they overwrite each other in
        every join the report does. The load point comes from what the harness
        recorded, not from what was asked for: in a self-calibrating sweep those
        differ, and the harness is right. Two points that report the same load are
        told apart by their repeat number.
        """
        load = cell.load
        reported = emission.load or {}
        if reported.get("kind") == "concurrency" and reported.get("concurrency"):
            load = replace(load, concurrency=int(reported["concurrency"]))
        elif reported.get("request_rate"):
            load = replace(load, request_rate=float(reported["request_rate"]))
        point = replace(cell, load=load)
        while point.id in taken:
            point = replace(point, repeat=point.repeat + 1)
        return point

    def apply_derived(self, measurement: Measurement, summary) -> None:
        """Fill the fields this harness does not report, and say that we did."""
        derived = metrics.derive(summary, measurement.readings)
        for field_id, (value, how) in derived.items():
            measurement.readings[field_id] = value
            measurement.derived[field_id] = how
            if field_id in measurement.missing:
                measurement.missing.remove(field_id)

    def cell_timeout(self, cell: Cell) -> int:
        """A ceiling generous enough to be a bug detector, not a load knob."""
        budget = cell.load.seconds * 3 if cell.load.seconds else 0
        return max(self.options.timeout, budget)

    # ── stages ───────────────────────────────────────────────────────────────

    def matrix(self) -> None:
        """Every cell in the plan, in order."""
        cells = [cell for cell in expand(self.plan) if self.wanted(cell)]
        self.say(f"{len(cells)} measurements to take")
        for index, cell in enumerate(cells, start=1):
            self.say(f"[{index}/{len(cells)}] {cell.harness} · {cell.model.name} · {cell.load.label}")
            try:
                self.guard(cell)
                self.run_cell(cell)
            except subprocess.TimeoutExpired:
                self.say(f"  timed out after {self.cell_timeout(cell)}s")
                self.store.record(
                    Measurement.of(
                        cell,
                        status="timeout",
                        started_at=now(),
                        duration_seconds=float(self.cell_timeout(cell)),
                        command="",
                        artifacts_dir=str(self.store.cell_dir(cell)),
                        notes=[
                            "the harness did not finish inside the timeout; its container was killed "
                            "and what it printed until then is in harness.log"
                        ],
                    )
                )
            except HarnessError as exc:
                if not self.options.keep_going:
                    raise
                self.say(f"  {exc}")
            if index < len(cells) and not self.options.dry_run:
                time.sleep(self.plan.cooldown_seconds)

    def sweeps(self) -> None:
        """Every ladder a harness walks in one process.

        One invocation produces several measurements, each labelled with the load
        point the harness says it ran, which in a self-calibrating sweep is a rate
        the tool chose at runtime and nobody could have written in the plan.
        """
        specs = [spec for spec in sweep_specs(self.plan) if self.sweep_wanted(spec)]
        if not specs:
            return
        self.say(f"{len(specs)} ladder runs")
        for index, spec in enumerate(specs, start=1):
            harness = registry.get(spec.harness)
            if not harness.sweeps:
                self.say(f"  {spec.harness} cannot walk a ladder in one process; skipping")
                continue
            rungs = ", ".join(f"c{rung}" for rung in spec.ladder)
            self.say(f"[{index}/{len(specs)}] {spec.harness} · {spec.model.name} · {rungs}")
            self.run_sweep(spec, harness)
            if index < len(specs) and not self.options.dry_run:
                time.sleep(self.plan.cooldown_seconds)

    def run_sweep(self, spec, harness: Harness) -> None:
        """One ladder run, recorded as one measurement per rung."""
        results_dir = self.store.sweep_dir(spec)
        ctx = Context(
            endpoint_url=self.plan.endpoint.url,
            token=self.token,
            hf_cache=self.options.hf_cache,
            results_dir=results_dir,
            image=self.images[spec.harness],
        )
        if not spec.model.tokenizer:
            self.say(f"  {spec.model.name} has no tokenizer; skipping")
            return
        invocation = harness.sweep_command(ctx, spec)
        if self.options.dry_run:
            self.say(f"  would run: {invocation.display}")
            return
        started = now()
        log = results_dir / "harness.log"
        try:
            returncode, elapsed = self.execute(invocation, log, self.options.timeout)
        except subprocess.TimeoutExpired:
            self.say(f"  timed out after {self.options.timeout}s")
            returncode, elapsed = -1, float(self.options.timeout)
        status = "ok" if returncode == 0 else f"exit {returncode}"

        try:
            emissions = harness.sweep_read(ctx, spec)
        except (HarnessError, ValueError) as exc:
            self.say(f"  {exc}")
            self.store.record(
                Measurement.of(
                    self.sweep_cell(spec, spec.ladder[0]),
                    status="unreadable" if returncode == 0 else status,
                    started_at=started,
                    duration_seconds=round(elapsed, 3),
                    command=invocation.display,
                    artifacts_dir=str(results_dir),
                    notes=[str(exc)],
                )
            )
            return

        for emission in emissions:
            reported = emission.load or {}
            rung = int(reported.get("concurrency") or spec.ladder[0])
            cell = self.sweep_cell(
                spec,
                rung,
                request_rate=float(reported.get("request_rate") or 0.0),
                repeat=int(reported.get("run_number") or 0) + 1,
            )
            measurement = Measurement.of(
                cell,
                status=status,
                started_at=started,
                duration_seconds=round(elapsed, 3),
                command=invocation.display,
                artifacts_dir=str(results_dir),
                harness_version=emission.version,
                summary=emission.summary,
                notes=emission.notes,
            )
            self.apply_derived(measurement, emission.summary)
            measurement.errors = int(measurement.readings.get(metrics.ERROR_COUNT, 0))
            self.grade(measurement)
            self.store.record(measurement)
            self.say(f"  {emission.load_label or f'c{rung}'}:")
            self.report_cell(measurement)

    def sweep_cell(self, spec, rung: int, request_rate: float = 0.0, repeat: int = 1) -> Cell:
        """A cell standing for one rung of a ladder, so the store shape stays one shape."""
        return Cell(
            harness=spec.harness,
            model=spec.model,
            load=Load(
                concurrency=rung,
                requests=spec.requests_per_rung,
                seconds=spec.seconds_per_rung,
                request_rate=request_rate,
            ),
            repeat=repeat,
            input_tokens=spec.input_tokens,
            output_tokens=spec.output_tokens,
            stage="sweep",
        )

    def knee(self) -> None:
        """Climb until the endpoint stops meeting the SLA, then stop.

        The last rung that held is the answer. The rung that broke is kept too, and
        labelled as the breach: a ceiling with no evidence of what happens above it
        is a number nobody can act on.
        """
        knee = self.plan.knee
        if not knee.enabled:
            return
        if not self.options.hard:
            self.say("skipping the saturation search: the plan enables it, this invocation did not (--hard)")
            return
        for model in self.plan.models:
            if self.options.only_model and model.name not in self.options.only_model:
                continue
            self.say(f"saturation search for {model.name}: {knee.field} <= {knee.limit:g}")
            points: list[dict[str, object]] = []
            passing: int | None = None
            breach: dict[str, object] | None = None
            for cell in knee_cells(self.plan, model):
                self.say(f"  rung c{cell.load.concurrency}")
                try:
                    self.guard(cell)
                    taken = self.run_cell(cell)
                except (subprocess.TimeoutExpired, HarnessError) as exc:
                    breach = {"concurrency": cell.load.concurrency, "why": f"the harness failed: {exc}"}
                    break
                if not taken:
                    continue
                measurement = taken[0]
                value = measurement.readings.get(knee.field)
                errors = measurement.errors
                points.append(
                    {
                        "concurrency": cell.load.concurrency,
                        "value": value,
                        "errors": errors,
                        "cell_id": measurement.cell_id,
                    }
                )
                if value is None:
                    breach = {
                        "concurrency": cell.load.concurrency,
                        "why": f"{knee.field} was not in this harness's export, so the SLA cannot be judged",
                    }
                    break
                if errors > knee.error_limit:
                    breach = {
                        "concurrency": cell.load.concurrency,
                        "why": f"{errors} requests failed",
                        "value": value,
                    }
                    break
                if value > knee.limit:
                    breach = {
                        "concurrency": cell.load.concurrency,
                        "why": f"{knee.field} reached {value:.0f}, over the {knee.limit:g} limit",
                        "value": value,
                    }
                    break
                passing = cell.load.concurrency
                if not self.options.dry_run:
                    time.sleep(self.plan.cooldown_seconds)
            self.store.suite.knee[model.name] = {
                "harness": knee.harness,
                "field": knee.field,
                "limit": knee.limit,
                "error_limit": knee.error_limit,
                "ladder": list(knee.ladder),
                "points": points,
                "max_passing": passing,
                "breach": breach,
            }
            self.store.flush()
            if passing is None:
                self.say(f"  {model.name} met the SLA at no rung on this ladder")
            else:
                self.say(f"  {model.name} holds the SLA up to concurrency {passing}")

    # ── plumbing ─────────────────────────────────────────────────────────────

    def sweep_wanted(self, spec) -> bool:
        """Whether `--harness` and `--model` leave this ladder run in the suite."""
        return self.selected(spec.harness, spec.model.name)

    def wanted(self, cell: Cell) -> bool:
        """Whether `--harness` and `--model` leave this cell in the suite."""
        return self.selected(cell.harness, cell.model.name)

    def selected(self, harness: str, model: str) -> bool:
        if self.options.only_harness and harness not in self.options.only_harness:
            return False
        return not (self.options.only_model and model not in self.options.only_model)

    def guard(self, cell: Cell) -> None:
        """Refuse a cell a harness cannot express, before it fails inside a container."""
        check_cell(cell)

    def report_cell(self, measurement: Measurement) -> None:
        ttft = measurement.readings.get(metrics.TTFT_P90)
        speed = measurement.readings.get(metrics.OUTPUT_SPEED_PER_STREAM)
        total = measurement.readings.get(metrics.OUTPUT_SPEED_TOTAL)
        bits = [f"{measurement.status}"]
        if ttft is not None:
            bits.append(f"ttft p90 {ttft:.0f} ms")
        if speed is not None:
            bits.append(f"{speed:.0f} tok/s/stream")
        if total is not None:
            bits.append(f"{total:.0f} tok/s total")
        if measurement.errors:
            bits.append(f"{measurement.errors} errors")
        self.say("  " + " · ".join(bits))

    def say(self, message: str) -> None:
        if not self.options.quiet:
            print(message, flush=True)

    # ── entry ────────────────────────────────────────────────────────────────

    def run(self) -> None:
        """The whole suite: token, probe, images, matrix, saturation search.

        A dry run reads the token from the environment and nothing else: it sends
        nothing to the endpoint, asks no registry for a digest and pulls or builds no
        image, so the commands it prints name images by their pinned index digest.
        """
        refuse_invalid(self.plan)
        self.token = self.resolve_token()
        if not self.options.dry_run:
            self.probe()
        harnesses = sorted({cell.harness for cell in expand(self.plan) if self.wanted(cell)})
        ladders = {spec.harness for spec in sweep_specs(self.plan) if self.sweep_wanted(spec)}
        harnesses = sorted(set(harnesses) | ladders)
        if self.plan.knee.enabled and self.options.hard:
            harnesses = sorted(set(harnesses) | {self.plan.knee.harness})
        if not self.options.dry_run:
            self.prepare_images(harnesses)
        else:
            self.images = {name: registry.get(name).reference for name in harnesses}
        self.store.suite.notes.extend(
            f"{name}: {registry.get(name).image} → {self.images.get(name, 'unresolved')}" for name in harnesses
        )
        try:
            self.matrix()
            self.sweeps()
            self.knee()
        except KeyboardInterrupt:
            self.store.suite.notes.append("interrupted: the cells below are every one that finished")
            self.say("\ninterrupted: flushing what is on disk")
        finally:
            self.store.suite.finished_at = now()
            self.store.flush()


def check_cell(cell: Cell) -> None:
    """Refuse a cell a harness cannot express, before it fails inside a container."""
    if cell.load.requests <= 0 and cell.load.seconds <= 0:
        raise HarnessError(f"{cell.id}: a load point needs either `requests` or `seconds`")
    if cell.harness in {"aiperf", "vllm-bench"} and cell.load.requests <= 0:
        raise HarnessError(f"{cell.id}: {cell.harness} counts requests, not seconds: give the load point a `requests`")
    if not cell.model.tokenizer:
        raise HarnessError(
            f"{cell.model.name} has no tokenizer. Every one of these harnesses counts tokens "
            "client-side and defaults the tokenizer to the served name, which is not a "
            "Hugging Face repository: the run dies before it sends a request."
        )


def plan_problems(plan: Plan) -> list[str]:
    """Everything in the plan that would fail mid-run, found before the first request.

    A plan is checked whole, ignoring `--harness` and `--model`, because a plan
    is a committed document and one that is wrong for a harness nobody selected
    today is still wrong.
    """
    found: list[str] = []
    if plan.repeats < 1:
        found.append(f"repeats is {plan.repeats}; it must be at least 1")
    names = set(plan.harnesses) | set(plan.sweep.harnesses if plan.sweep.enabled else ())
    if plan.knee.enabled:
        names.add(plan.knee.harness)
    for name in sorted(names):
        try:
            registry.get(name)
        except KeyError as exc:
            found.append(str(exc.args[0]))
    if found:
        return found
    for load in plan.load:
        if load.concurrency < 1:
            found.append(f"load point {load.label}: concurrency must be at least 1")
        if load.requests < 0 or load.seconds < 0 or load.request_rate < 0:
            found.append(f"load point {load.label}: requests, seconds and request_rate cannot be negative")
    cells = expand(plan)
    if plan.knee.enabled:
        if plan.knee.field not in metrics.UNITS:
            found.append(f"knee field {plan.knee.field!r} is not a metric id; one of {', '.join(metrics.UNITS)}")
        for model in plan.models:
            cells += knee_cells(plan, model)
    for cell in cells:
        try:
            check_cell(cell)
        except HarnessError as exc:
            found.append(str(exc))
    for spec in sweep_specs(plan):
        if not registry.get(spec.harness).sweeps:
            found.append(f"{spec.harness} is in [sweep] and cannot walk a ladder in one process")
        if spec.requests_per_rung <= 0 and spec.seconds_per_rung <= 0:
            found.append(f"{spec.id}: a ladder needs requests_per_rung or seconds_per_rung")
        if not spec.model.tokenizer:
            found.append(f"{spec.model.name} has no tokenizer")
    return list(dict.fromkeys(found))


def refuse_invalid(plan: Plan) -> None:
    """Raise with every problem in the plan at once, instead of the first one mid-run."""
    problems = plan_problems(plan)
    if problems:
        raise ValueError(f"{plan.source or plan.name} cannot run:\n  " + "\n  ".join(problems))
