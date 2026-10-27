from __future__ import annotations

import datetime as dt

from typing import Any
from typing import NamedTuple

from sqlalchemy import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import OPERATION
from app.core.ids import BENCHMARK_RUN
from app.core.ids import BENCHMARK_REQUEST
from app.core.ids import new_uid
from app.db.models import Artifact
from app.db.models import Workload
from app.db.models import Operation
from app.db.models import BenchmarkRun
from app.db.models import BenchmarkRequest
from app.db.notify import OPERATION as OPERATION_KIND
from app.db.notify import Notifier
from app.db.notify import DeclaredChange
from app.db.notify import DatabaseNotifier
from app.core.schemas import ListResponse
from app.core.serving import served_model
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor
from app.modules.operations.services import PHASES
from app.modules.operations.services import PENDING

from . import curves
from . import buckets
from . import verdicts
from . import harnesses
from . import summaries
from .schemas import BenchmarkRunSchema
from .schemas import BenchmarkBandSchema
from .schemas import BenchmarkKneeSchema
from .schemas import BenchmarkCheckSchema
from .schemas import BenchmarkCurveSchema
from .schemas import BenchmarkHarnessSchema
from .schemas import BenchmarkReadingSchema
from .schemas import BenchmarkRequestSchema
from .schemas import BenchmarkVerdictSchema
from .schemas import BenchmarkCoverageSchema
from .schemas import BenchmarkParameterSchema
from .schemas import BenchmarkCurvePointSchema
from .schemas import BenchmarkProvenanceSchema
from .schemas import BenchmarkRunCreateRequest
from .schemas import BenchmarkRequestCreateRequest
from .schemas import ServingBenchmarkSummarySchema
from .repository import BenchmarkRepository


OPERATION_TYPE = "deployment.benchmark"
"""The operation that carries a run out.

A benchmark is imperative, long-running and able to fail, which is the third kind
of thing and already has machinery: phases, a deadline, cancellation, and replay
to a cluster that reconnected mid-run. Building a second lifecycle beside it would
be building all of that again, worse. What `Operation` cannot hold is the result —
its `result` is a flat string map on the wire and a harness emits nested JSON — so
the run is its own object and the operation is how it gets done.

The two new harness kinds ride this same operation and the same `benchmark.observed`
frame, as new values of the `harness` parameter. A conformance suite and an accuracy
eval are not new kinds of errand; they are different tools pointed at a serving
surface, and giving each its own operation type would be three lifecycles where the
difference between them is one string.
"""

SINGLE = "single"
WARMUP = "warmup"
RAMP = "ramp"
SWEEP = "sweep"
SOAK = "soak"

MODES: tuple[str, ...] = (SINGLE, WARMUP, RAMP, SWEEP, SOAK)
"""A shape of runs over one harness, and deliberately not a harness of its own.

A `soak` harness and a `ramp` harness would put the same AIPerf parameters in three
tables and make two runs of one tool incomparable for no reason. Modes do not compose
either: a request has one, and a full acceptance pass is three requests rather than a
fourth object grouping them.
"""

STEPPED: dict[str, str] = {RAMP: "concurrency", SWEEP: "input_tokens_mean"}
"""Which harness parameter a stepped mode walks.

A ramp walks load, which is what "hold each concurrency for ten minutes and collect
after stabilisation" means. A sweep walks prompt length, which is what makes a
per-band time-to-first-token claim evidenceable at all — the standard sets a separate
target for each input-length band and one aggregate number answers none of them.
"""


class RunNames(NamedTuple):
    """The public ids a run is rendered with, resolved by join rather than by trip."""

    deployment: str | None
    cluster: str | None
    endpoint: str | None
    operation: str | None
    request: str | None


class Target(NamedTuple):
    """One thing a request expands a run against, with the provenance it will snapshot."""

    deployment: Any
    workload: Any
    artifact: Any
    cluster_uid: str | None = None


class Expansion(NamedTuple):
    """Everything one request settles before it knows how many runs it becomes."""

    data: BenchmarkRequestCreateRequest
    harness: harnesses.Harness
    image: str
    parameters: dict
    endpoint: Any


class Slot(NamedTuple):
    """One run's place in an expansion: which target, at which step, in which order."""

    target: Target
    step: int | None
    index: int


class BenchmarkService:
    """Writes the declared half of a request, its runs, and the operations that perform them.

    One transaction for all of them, because a run with no operation is a
    measurement nothing will ever take, an operation with no run is a result with
    nowhere to land, and a run with no request is a band of a sweep that cannot be
    re-aggregated with the others. Everything after creation — phase, summary, the
    timestamps — is ingestion's, applied from `benchmark.observed`. The verdict is
    neither writer's: it is derived at read against the criteria version the request
    recorded.
    """

    def __init__(self, db: AsyncSession, actor: str = "unknown", notifier: Notifier | None = None) -> None:
        self.db = db
        self.actor = actor
        self.repo = BenchmarkRepository(db)
        self.notifier: Notifier = DatabaseNotifier() if notifier is None else notifier

    # ----------------------------------------------------------------- harnesses

    def harnesses(self) -> list[BenchmarkHarnessSchema]:
        """The harnesses, the levels they grade at, and their knobs.

        The console would otherwise carry a second copy of both tables, and a
        second copy is a second answer to what a probe suite may be pointed at.
        """
        return [
            BenchmarkHarnessSchema(
                name=harness.name,
                image=harness.image,
                description=harness.description,
                levels=list(harness.levels),
                max_probes=harness.max_probes,
                parameters=[
                    BenchmarkParameterSchema(
                        name=parameter.name,
                        type=parameter.type.__name__,
                        default=parameter.default,
                        description=parameter.description,
                        levels=list(parameter.levels or harness.levels),
                    )
                    for parameter in harness.parameters
                ],
            )
            for harness in harnesses.HARNESSES.values()
        ]

    # ----------------------------------------------------------------- reads

    def schema(self, run: BenchmarkRun, names: RunNames) -> BenchmarkRunSchema:
        return BenchmarkRunSchema(
            id=run.uid,
            request_id=names.request,
            deployment_id=names.deployment,
            cluster_id=names.cluster,
            endpoint_id=names.endpoint,
            operation_id=names.operation,
            harness=run.harness,
            harness_version=run.harness_version,
            image=run.image,
            parameters=run.parameters or {},
            step_index=run.step_index,
            vantage=run.vantage,
            discard=run.discard,
            requested_by=run.requested_by,
            reason=run.reason,
            provenance=BenchmarkProvenanceSchema(
                model=run.model,
                deployment_generation=run.deployment_generation,
                serving_digest=run.serving_digest,
                engine_args=run.engine_args or {},
                serving=run.serving or {},
            ),
            phase=run.phase,
            summary=run.summary,
            artifacts_uri=run.artifacts_uri,
            error=run.error,
            log_tail=run.log_tail,
            created_at=run.created_at,
            updated_at=run.updated_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )

    def _row_schema(self, row: Row) -> BenchmarkRunSchema:
        return self.schema(
            row[0],
            RunNames(
                deployment=row.deployment_uid,
                cluster=row.cluster_uid,
                endpoint=row.endpoint_uid,
                operation=row.operation_uid,
                request=row.request_uid,
            ),
        )

    async def get(self, ref: str) -> BenchmarkRunSchema:
        row = await self.repo.get(ref)
        if row is None:
            raise NotFound(f"no benchmark run {ref!r}")
        return self._row_schema(row)

    async def serving_summary(self, digest: str) -> ServingBenchmarkSummarySchema:
        """Peak per-stream output-token throughput across one configuration's runs.

        Anchored on `serving_digest` because that is what a measurement is about:
        the digest is the content of the configuration, so every run that shares
        it measured the same running process and belongs in the same population.
        Anchoring on the workload instead would split that population whenever the
        same configuration was declared twice, and would lose the runs outright
        when the workload was deleted. It replaces a rollup keyed on a recipe id
        and version, which named a row that could be edited under the numbers.

        Offline benchmark measurement, read through the same harness-and-version
        adapter the verdicts use. A run whose harness or version has no adapter,
        or whose summary lacks the throughput path, contributes no reading and is
        left out of `graded_run_count` — the peak is the maximum of what could be
        read, or `None` when nothing could.

        An unknown digest is not an error. It is a configuration nothing has
        measured yet, which is a zero count and a named absence rather than a 404
        about an object that was never an object.
        """
        rows = await self.repo.serving_runs(digest)
        peak: float | None = None
        graded = 0
        for row in rows:
            adapter = summaries.adapter_for(row.harness, row.harness_version)
            if adapter is None:
                continue
            reading = summaries.read(adapter, row.summary, summaries.OUTPUT_SPEED_PER_STREAM)
            if reading.value is None:
                continue
            graded += 1
            peak = reading.value if peak is None else max(peak, reading.value)
        return ServingBenchmarkSummarySchema(
            serving_digest=digest,
            run_count=len(rows),
            graded_run_count=graded,
            peak_output_tokens_per_second=peak,
        )

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        deployment: str | None = None,
        cluster: str | None = None,
        harness: str | None = None,
        phase: str | None = None,
        actor: str | None = None,
        request: str | None = None,
        since: dt.datetime | None = None,
    ) -> ListResponse[BenchmarkRunSchema]:
        if harness is not None and harness not in harnesses.HARNESSES:
            raise Invalid(f"unknown harness {harness!r}", {"allowed": sorted(harnesses.HARNESSES)})
        if phase is not None and phase not in PHASES:
            raise Invalid(f"unknown phase {phase!r}", {"allowed": sorted(PHASES)})
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            before_seq=decode_cursor(cursor),
            deployment=deployment,
            cluster=cluster,
            harness=harness,
            phase=phase,
            actor=actor,
            request=request,
            since=since,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        return ListResponse(items=[self._row_schema(row) for row in kept], count=len(kept), next_cursor=next_cursor)

    # ----------------------------------------------------------------- verdicts

    async def verdict(self, ref: str, criteria: str | None = None) -> BenchmarkVerdictSchema:
        """Whether one run met the standard, derived now and stored nowhere.

        `criteria` defaults to the version the request was created under, which
        is what makes the answer stable. Passing another asks the genuinely
        useful question a stored verdict cannot answer at all — would this run
        have passed under the revision that came later.
        """
        facts, recorded = await self._facts(ref)
        entry = verdicts.resolve_criteria(criteria or recorded)
        return _verdict_schema(verdicts.verdict(facts, entry))

    async def request_verdict(self, ref: str, criteria: str | None = None) -> BenchmarkVerdictSchema:
        """A request's verdict, which is the rollup of its runs' verdicts under one rule."""
        request = await self.repo.request(ref)
        if request is None:
            raise NotFound(f"no benchmark request {ref!r}")
        entry = verdicts.resolve_criteria(criteria or request[0].criteria_version)
        rows = await self.repo.request_facts(ref)
        baselines = await self.repo.baselines_by_artifact({row.artifact_id for row in rows})
        graded: list[verdicts.Verdict] = []
        for row in rows:
            facts, _ = _run_facts(row, baselines.get(row.artifact_id, []))
            if facts.discard:
                continue
            graded.append(verdicts.verdict(facts, entry))
        return BenchmarkVerdictSchema(
            outcome=verdicts.rollup_verdicts(graded),
            criteria_version=entry.version,
            reason=None if graded else "the request has no run that grades",
            checks=[check for one in graded for check in _check_schemas(one)],
        )

    async def bands(self, ref: str) -> ListResponse[BenchmarkBandSchema]:
        """One request's runs, re-aggregated by the input-length band each landed in.

        The band comes from each run's own declared prompt length and never from
        its summary: the summary is verbatim and is under no obligation to name a
        band, or to name it the same way twice.
        """
        request = await self.repo.request(ref)
        if request is None:
            raise NotFound(f"no benchmark request {ref!r}")
        runs = await self.repo.request_runs(ref)
        reports = buckets.reaggregate([buckets.sample(run) for run in runs])
        items = [
            BenchmarkBandSchema(
                band=report.band,
                runs=report.runs,
                requests=report.requests,
                ttft_p90_ms=report.ttft_p90_ms,
                ttft_p50_ms=report.ttft_p50_ms,
                ttft_avg_ms=report.ttft_avg_ms,
                incomplete=list(report.incomplete),
            )
            for report in reports
        ]
        return ListResponse(items=items, count=len(items), next_cursor=None)

    async def curve(self, ref: str, criteria: str | None = None) -> BenchmarkCurveSchema:
        """One request's runs read as a load curve, with the knee derived now.

        A single object rather than a `ListResponse` of points, because the
        criteria version the knee was derived under belongs to the answer and a
        list envelope has nowhere to carry it — the same reason `/verdict` is
        shaped this way.

        The step field comes from the mode the request was created under and
        never from what the runs happen to carry: a ramp whose steps all
        resolved to the same concurrency is still a ramp, and reading the axis
        off the data would make it a sweep.
        """
        row = await self.repo.request(ref)
        if row is None:
            raise NotFound(f"no benchmark request {ref!r}")
        request = row[0]
        standard = verdicts.resolve_criteria(criteria or request.criteria_version)
        step_field = curves.STEP_FIELDS.get(request.mode, curves.CONCURRENCY)
        runs = await self.repo.request_runs(ref)
        points = curves.points(runs, step_field)
        return BenchmarkCurveSchema(
            request=request.uid,
            serving_digest=_one_digest(runs),
            step_field=step_field,
            criteria_version=standard.version,
            points=[_curve_point_schema(point) for point in points],
            knee=_knee_schema(curves.knee(points, standard, step_field)),
        )

    async def get_request(self, ref: str) -> BenchmarkRequestSchema:
        row = await self.repo.request(ref)
        if row is None:
            raise NotFound(f"no benchmark request {ref!r}")
        runs = await self.repo.request_runs(ref)
        return _request_schema(row, runs)

    async def _facts(self, ref: str) -> tuple[verdicts.RunFacts, str]:
        row = await self.repo.facts(ref)
        if row is None:
            raise NotFound(f"no benchmark run {ref!r}")
        return _run_facts(row, await self.repo.baselines(row.artifact_id))

    # ----------------------------------------------------------------- writes

    async def create(self, data: BenchmarkRunCreateRequest) -> BenchmarkRunSchema:
        """The deployment-level shortcut, which is what every existing caller posts.

        It expands through the same path a levelled request does rather than
        beside it: two creation paths would be two answers to what a run
        snapshots.
        """
        created = await self.create_request(
            BenchmarkRequestCreateRequest(
                level=harnesses.DEPLOYMENT,
                target=data.deployment,
                harness=data.harness,
                image=data.image,
                criteria=data.criteria,
                tier=data.tier,
                reason=data.reason,
                parameters=data.parameters,
                deadline=data.deadline,
            )
        )
        return await self.get(created.runs[0])

    async def create_request(self, data: BenchmarkRequestCreateRequest) -> BenchmarkRequestSchema:
        """Records what is to be measured, expands it into runs, and asks for each one.

        The provenance snapshot is taken here and not by the operator. What a
        deployment was running is a fact the plane already holds, and reading it
        at creation is what pins a run to a generation rather than to whatever
        the deployment happens to be by the time the job finishes.

        Expansion is deterministic and happens once. A deployment-level request
        is one run; a workload-level request is one run per deployment placement,
        each snapshotting its own; an endpoint-level request is exactly one run
        with an empty snapshot, because a request to a public name is routed by
        weight across every enabled upstream and a run attributed to one of them
        would be a confident wrong attribution rather than an honest absent one.

        Endpoint-level requests are refused for now, after the harness and level
        checks so the error says the more specific thing first. Nothing executes
        them: a cluster's benchmark job never dials out to a public name, and the
        executor that would own that surface is roadmap. Recorded, they sat
        `pending` for ever, which reads as a stuck cluster.
        """
        harness = harnesses.resolve(data.harness, data.level)
        if data.level == harnesses.ENDPOINT:
            raise Invalid(
                "endpoint-level benchmark runs are not supported yet: nothing executes them",
                {"level": data.level, "supported": [harnesses.DEPLOYMENT, harnesses.WORKLOAD]},
            )
        parameters = harnesses.normalise(harness, data.parameters or {}, data.level)
        image = harnesses.resolve_image(harness, data.image)
        criteria = verdicts.resolve_criteria(data.criteria)
        tier = _tier(data.tier)
        steps = _steps(harness, data)

        targets, endpoint, coverage = await self._expand(data)
        request = BenchmarkRequest(
            uid=new_uid(BENCHMARK_REQUEST),
            level=data.level,
            deployment_id=targets[0].deployment.id if data.level == harnesses.DEPLOYMENT else None,
            workload_id=targets[0].deployment.workload_id if data.level == harnesses.WORKLOAD else None,
            endpoint_id=endpoint.id if endpoint is not None else None,
            harness=harness.name,
            image=image,
            parameters=parameters,
            mode=data.mode,
            criteria_version=criteria.version,
            tier=tier,
            coverage=coverage,
            homogeneous=_homogeneous(coverage),
            requested_by=self.actor,
            reason=data.reason,
        )

        plan = Expansion(data=data, harness=harness, image=image, parameters=parameters, endpoint=endpoint)
        runs: list[BenchmarkRun] = []
        operations: list[tuple[Operation, BenchmarkRun]] = []
        for target in targets or [Target(None, None, None)]:
            for index, step in enumerate(steps):
                run, operation = self._expand_one(plan, Slot(target, step, index))
                runs.append(run)
                if operation is not None:
                    operations.append((operation, run))

        try:
            await self.repo.add_request(request)
            for operation, _ in operations:
                self.db.add(operation)
            await self.db.flush()
            for operation, run in operations:
                run.operation_id = operation.id
            for run in runs:
                run.request_id = request.id
                await self.repo.add(run)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("benchmark request could not be recorded") from exc

        announced: set[tuple[str, str]] = set()
        clusters = await self.repo.cluster_uids({op.cluster_id for op, _ in operations if op.cluster_id is not None})
        for operation, _ in operations:
            cluster_uid = clusters.get(operation.cluster_id)
            if cluster_uid is None or (cluster_uid, operation.uid) in announced:
                continue
            announced.add((cluster_uid, operation.uid))
            await self.notifier.changed(
                self.db,
                DeclaredChange(cluster_id=cluster_uid, kind=OPERATION_KIND, uid=operation.uid),
            )
        return await self.get_request(request.uid)

    def _expand_one(self, plan: Expansion, slot: Slot) -> tuple[BenchmarkRun, Operation | None]:
        """One run of one target at one step, and the errand that carries it out.

        An endpoint-level run gets no operation. The operator's benchmark job
        reaches its own cluster's inference endpoint and never dials out, which
        is the rule that keeps the data plane off this one — so a run against a
        public name is not an errand any cluster can be handed. `create_request`
        refuses that level until an executor for the public surface exists.
        """
        data, target, endpoint = plan.data, slot.target, plan.endpoint
        resolved = dict(plan.parameters)
        if slot.step is not None:
            resolved[STEPPED[data.mode]] = slot.step

        workload: Workload | None = target.workload
        artifact: Artifact | None = target.artifact
        known = workload is not None and artifact is not None
        model = served_model(workload, artifact) if known else ""
        if known and "tokenizer" in resolved and not resolved.get("tokenizer"):
            resolved["tokenizer"] = tokenizer_for(workload, artifact)

        # EvalScope's OpenAI client sends a strict `model` field. What the frontend answers to is
        # the artifact's HF repo id — not the workload's served name, which is what `model` carries.
        # `tokenizer_for` already knows how to resolve that id from the artifact's location, so the
        # harness gets it under a separate knob and the label the operator renders stays the served
        # name.
        endpoint_model = model
        if known and plan.harness.name == harnesses.EVALSCOPE.name:
            endpoint_model = tokenizer_for(workload, artifact) or model

        run_uid = new_uid(BENCHMARK_RUN)
        run = BenchmarkRun(
            uid=run_uid,
            deployment_id=target.deployment.id if target.deployment is not None else None,
            endpoint_id=endpoint.id if endpoint is not None else None,
            cluster_id=target.deployment.cluster_id if target.deployment is not None else None,
            harness=plan.harness.name,
            image=plan.image,
            parameters=resolved,
            requested_by=self.actor,
            reason=data.reason,
            model=model,
            deployment_generation=target.deployment.generation if target.deployment is not None else 0,
            serving_digest=workload.serving_digest if workload is not None else None,
            engine_args=dict(workload.engine_args or {}) if workload is not None else {},
            serving=dict(workload.serving or {}) if workload is not None else {},
            step_index=slot.index,
            discard=data.mode == WARMUP,
            phase=PENDING,
        )

        if target.deployment is None:
            return run, None

        operation = Operation(
            uid=new_uid(OPERATION),
            cluster_id=target.deployment.cluster_id,
            type=OPERATION_TYPE,
            target_type="deployment",
            target_id=target.deployment.id,
            phase=PENDING,
            requested_by=self.actor,
            reason=data.reason,
            payload={
                "parameters": {
                    **harnesses.wire_parameters(plan.harness, resolved, plan.image, model, endpoint_model),
                    "benchmark_id": run_uid,
                    "deployment_id": target.deployment.uid,
                }
            },
            deadline_at=data.deadline,
        )
        return run, operation

    async def _expand(self, data: BenchmarkRequestCreateRequest) -> tuple[list[Target], Any, list[dict]]:
        if data.level == harnesses.DEPLOYMENT:
            return await self._deployment_target(data.target)
        if data.level == harnesses.WORKLOAD:
            return await self._workload_targets(data.target)
        return await self._endpoint_target(data.target)

    async def _deployment_target(self, ref: str) -> tuple[list[Target], None, list[dict]]:
        """The one deployment a reference names, or an error saying which way it failed.

        Same rule as an operation target: a name is unique per cluster and not
        above it, so an ambiguous one is refused rather than picked. Benchmarking
        an arbitrary one of two deployments that share a name would produce a
        number attributed to the wrong thing, which is worse than no number.
        """
        matches = await self.repo.deployment_targets(ref)
        if not matches:
            raise NotFound(f"no deployment {ref!r}")
        if len(matches) > 1:
            raise Invalid(
                f"deployment {ref!r} matches {len(matches)} objects; use its id",
                {"deployment": ref, "matches": sorted(row[0].uid for row in matches)},
            )
        row = matches[0]
        deployment = row[0]
        if not deployment.enabled:
            raise Conflict(
                f"deployment {deployment.uid} is disabled; there is nothing serving to measure",
                {"deployment": deployment.uid},
            )
        target = Target(deployment, row[1], row[2], row.cluster_uid)
        return [target], None, [_coverage(target)]

    async def _workload_targets(self, ref: str) -> tuple[list[Target], None, list[dict]]:
        """Every placement of one workload, each run snapshotting its own.

        A workload with no placement is refused rather than expanded to nothing:
        a request that produced no runs would sit for ever looking pending, and
        "placement has not run yet" is a different answer from "it failed".
        """
        matches = await self.repo.workload_targets(ref)
        if not matches:
            raise NotFound(f"no workload {ref!r}, or it has no placement to measure")
        names = {row.workload_uid for row in matches}
        if len(names) > 1:
            raise Invalid(
                f"workload {ref!r} matches {len(names)} objects; use its id",
                {"workload": ref, "matches": sorted(names)},
            )
        targets = [Target(row[0], row[1], row[2], row.cluster_uid) for row in matches if row[0].enabled]
        if not targets:
            raise Conflict(f"every deployment of workload {ref!r} is disabled", {"workload": ref})
        return targets, None, [_coverage(target) for target in targets]

    async def _endpoint_target(self, ref: str) -> tuple[list[Target], Any, list[dict]]:
        """One run against the public name, and a snapshot of everything behind it.

        The coverage list is where the per-upstream expansion went. A request to
        a public name is routed by weight across every enabled upstream, so N
        runs would be N times the intended concurrency and each would measure
        whatever the gateway chose rather than the upstream it was dispatched
        for. One run, and the coverage records what the name resolved to at the
        moment it was asked.
        """
        rows = await self.repo.endpoint_targets(ref)
        if not rows:
            raise NotFound(f"no endpoint {ref!r}")
        endpoint = rows[0][0]
        if not endpoint.enabled:
            raise Conflict(f"endpoint {endpoint.uid} is disabled", {"endpoint": endpoint.uid})
        coverage = [_coverage(Target(row[1], row[2], row[3], row.cluster_uid)) for row in rows if row[1] is not None]
        return [], endpoint, coverage


def _coverage(target: Target) -> dict:
    return {
        "deployment_id": target.deployment.uid if target.deployment is not None else None,
        "cluster_id": target.cluster_uid,
        "serving_digest": target.workload.serving_digest if target.workload is not None else None,
        "artifact_id": target.artifact.uid if target.artifact is not None else None,
    }


def _homogeneous(coverage: list[dict]) -> bool:
    """True when every entry agrees on artifact and serving configuration.

    An accuracy verdict over a heterogeneous coverage is not attributable at
    all — two upstreams running different quantizations produce one score
    belonging to neither — so this boolean is what turns that into a stated
    `inconclusive` rather than a number a reader is left to misuse.

    `serving_digest` is the whole configuration in one value, so this compares
    two fields where it used to compare three and is stricter for it: the pair it
    replaced named a recipe row that could have been edited between the two
    entries and still read as the same recipe at the same version.
    """
    keys = {(entry["artifact_id"], entry["serving_digest"]) for entry in coverage}
    return len(keys) <= 1


def _steps(harness: harnesses.Harness, data: BenchmarkRequestCreateRequest) -> list[int | None]:
    """The load points a mode walks, or a single unstepped run.

    A stepped mode over a harness with no such knob is refused here rather than
    silently collapsing to one run: a caller who asked for a sweep and got one
    point would have a verdict covering one band and a claim covering six.
    """
    if data.mode not in MODES:
        raise Invalid(f"unknown benchmark mode {data.mode!r}", {"allowed": list(MODES)})
    if data.mode not in STEPPED:
        if data.steps:
            raise Invalid(f"the {data.mode} mode is a single run and takes no steps", {"mode": data.mode})
        return [None]

    knob = STEPPED[data.mode]
    if knob not in {parameter.name for parameter in harness.parameters}:
        raise Invalid(
            f"{harness.name} has no {knob!r} to step, so it cannot be run in {data.mode} mode",
            {"harness": harness.name, "mode": data.mode},
        )
    if not data.steps:
        raise Invalid(f"the {data.mode} mode needs at least one step", {"mode": data.mode, "parameter": knob})
    if any(step <= 0 for step in data.steps):
        raise Invalid(f"a {data.mode} step must be positive", {"mode": data.mode, "steps": data.steps})
    ordered: list[int | None] = []
    for step in data.steps:
        if step not in ordered:
            ordered.append(step)
    return ordered


def _run_facts(row: Row, baselines: list) -> tuple[verdicts.RunFacts, str]:
    """A run's verdict inputs from a `facts_query` row and its artifact's baselines, with the criteria it recorded."""
    run = row[0]
    parameters = run.parameters or {}
    recorded = row.criteria_version or verdicts.DEFAULT_CRITERIA_VERSION
    facts = verdicts.RunFacts(
        run_id=run.uid,
        harness=run.harness,
        harness_version=run.harness_version,
        level=row.level or harnesses.DEPLOYMENT,
        phase=run.phase,
        discard=bool(run.discard),
        summary=run.summary,
        input_tokens=_int_or_none(parameters.get("input_tokens_mean")),
        parameter_count=_int_or_none(row.parameter_count),
        tier=row.tier,
        strict=bool(parameters.get("strict", True)),
        datasets=harnesses.resolve_datasets(str(parameters.get("datasets") or "")),
        baselines=tuple(
            verdicts.Baseline(
                dataset=baseline.dataset,
                metric=baseline.metric,
                value=float(baseline.value),
                tolerance_pct=float(baseline.tolerance_pct),
            )
            for baseline in baselines
        ),
        homogeneous=True if row.homogeneous is None else bool(row.homogeneous),
    )
    return facts, recorded


def _tier(tier: str | None) -> str | None:
    if tier is None or not str(tier).strip():
        return None
    normalised = str(tier).strip().upper()
    if normalised not in verdicts.TIERS:
        raise Invalid(f"unknown service tier {tier!r}", {"allowed": list(verdicts.TIERS)})
    return normalised


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _check_schemas(entry: verdicts.Verdict) -> list[BenchmarkCheckSchema]:
    return [
        BenchmarkCheckSchema(
            id=check.id,
            label=check.label,
            outcome=check.outcome,
            observed=check.observed,
            threshold=check.threshold,
            comparator=check.comparator,
            detail=check.detail,
        )
        for check in entry.checks
    ]


def _verdict_schema(entry: verdicts.Verdict) -> BenchmarkVerdictSchema:
    return BenchmarkVerdictSchema(
        outcome=entry.outcome,
        criteria_version=entry.criteria_version,
        reason=entry.reason,
        checks=_check_schemas(entry),
    )


def _curve_point_schema(point: curves.Point) -> BenchmarkCurvePointSchema:
    return BenchmarkCurvePointSchema(
        step_index=point.step_index,
        step_value=point.step_value,
        run=point.run_id,
        phase=point.phase,
        discard=point.discard,
        readings={
            key: BenchmarkReadingSchema(value=value.value, unit=value.unit, path=value.path)
            for key, value in point.readings.items()
        },
    )


def _knee_schema(knee: curves.Knee) -> BenchmarkKneeSchema:
    return BenchmarkKneeSchema(
        found=knee.found,
        step_index=knee.step_index,
        step_value=knee.step_value,
        method=knee.method,
        metric=knee.metric,
        threshold=knee.threshold,
        reason=knee.reason,
    )


def _one_digest(runs: list[BenchmarkRun]) -> str | None:
    """The serving configuration a request's runs all measured, or nothing.

    Nothing rather than the first of several: a curve drawn through two
    configurations describes a machine that does not exist, and naming one of
    them would attribute half the points to the wrong thing.
    """
    digests = {run.serving_digest for run in runs if run.serving_digest}
    return digests.pop() if len(digests) == 1 else None


def _request_schema(row: Row, runs: list[BenchmarkRun]) -> BenchmarkRequestSchema:
    request = row[0]
    target = row.deployment_uid or row.workload_uid or row.endpoint_uid or ""
    return BenchmarkRequestSchema(
        id=request.uid,
        level=request.level,
        target_id=target,
        harness=request.harness,
        image=request.image,
        parameters=request.parameters or {},
        mode=request.mode,
        criteria_version=request.criteria_version,
        tier=request.tier,
        homogeneous=request.homogeneous,
        coverage=[
            BenchmarkCoverageSchema(
                deployment_id=entry.get("deployment_id"),
                cluster_id=entry.get("cluster_id"),
                serving_digest=entry.get("serving_digest"),
                artifact_id=entry.get("artifact_id"),
            )
            for entry in request.coverage or []
        ],
        requested_by=request.requested_by,
        reason=request.reason,
        phase=request_phase([run.phase for run in runs]),
        runs=[run.uid for run in runs],
        created_at=request.created_at,
        updated_at=request.updated_at,
    )


def request_phase(phases: list[str]) -> str:
    """A request's phase, derived from its runs and stored nowhere.

    Running while any run is still open, failed if any failed, succeeded when
    every one of them did. A column here would be a third writer on a lifecycle
    that already has two, and it would have no frame behind it.
    """
    if not phases:
        return PENDING
    if any(phase in ("pending", "running") for phase in phases):
        return "running" if any(phase == "running" for phase in phases) else PENDING
    for terminal in ("failed", "timed_out", "cancelled"):
        if any(phase == terminal for phase in phases):
            return terminal
    return "succeeded"


def tokenizer_for(workload: Workload, artifact: Artifact) -> str:
    """The repository AIPerf should load a tokenizer from, or nothing if we cannot say.

    Most authoritative first, and the served model name is nowhere in the list —
    it is what AIPerf falls back to on its own and it is the wrong answer: a
    deployment served as `qwen3.8-27b` is not a Hugging Face repository and the
    lookup fails before a single request is sent.

    An explicit `--tokenizer` wins, whether it reached the workload hyphenated
    the way discovery writes engine arguments or bare the way a hand-written
    one does, because it is the engine being told which tokenizer it is using. The
    artifact's metadata carries the same argument when discovery read it off a
    running workload.

    Failing that, the artifact's `hf://` location, because that is the same value
    the operator resolves `--model` to when it renders the engine — the tokenizer
    the engine loaded by default is the one in the repository it pulled from, and
    reading it from the same field keeps the two from drifting. The `hf:` digest
    is the last resort, for an artifact adopted by reference that never had its
    bytes resolved to a location.

    An empty string when nothing names a repository, which leaves AIPerf on its
    own default rather than inventing an id — a run that fails on a missing
    tokenizer is clearer than one that silently counted tokens with the wrong one.
    """
    engine_args = workload.engine_args or {}
    metadata = artifact.metadata_ or {}
    for declared in (
        engine_args.get("tokenizer"),
        engine_args.get("--tokenizer"),
        metadata.get("tokenizer"),
    ):
        if declared and str(declared).strip():
            return str(declared).strip()

    for location in artifact.locations or []:
        uri = str(location.uri or "")
        if uri.startswith("hf://"):
            return uri[len("hf://") :].split("@", 1)[0]

    digest = str(artifact.digest or "")
    if digest.startswith("hf:"):
        return digest[3:].split("@", 1)[0]
    name = str(artifact.name or "")
    return name if "/" in name else ""
