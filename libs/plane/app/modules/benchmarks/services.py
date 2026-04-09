import datetime as dt

from sqlalchemy import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import OPERATION
from app.core.ids import BENCHMARK_RUN
from app.core.ids import new_uid
from app.db.models import Recipe
from app.db.models import Artifact
from app.db.models import Operation
from app.db.models import BenchmarkRun
from app.db.notify import OPERATION as OPERATION_KIND
from app.db.notify import Notifier
from app.db.notify import DeclaredChange
from app.db.notify import DatabaseNotifier
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor
from app.modules.metrics.services import served_model
from app.modules.operations.services import PHASES
from app.modules.operations.services import PENDING

from . import harnesses
from .schemas import BenchmarkRunSchema
from .schemas import BenchmarkHarnessSchema
from .schemas import BenchmarkParameterSchema
from .schemas import BenchmarkProvenanceSchema
from .schemas import BenchmarkRunCreateRequest
from .repository import BenchmarkRepository


OPERATION_TYPE = "deployment.benchmark"
"""The operation that carries a run out.

A benchmark is imperative, long-running and able to fail, which is the third kind
of thing and already has machinery: phases, a deadline, cancellation, and replay
to a cluster that reconnected mid-run. Building a second lifecycle beside it would
be building all of that again, worse. What `Operation` cannot hold is the result —
its `result` is a flat string map on the wire and a harness emits nested JSON — so
the run is its own object and the operation is how it gets done.
"""


class BenchmarkService:
    """Writes the declared half of a run and the operation that performs it, together.

    One transaction for both rows, because a run with no operation is a
    measurement nothing will ever take, and an operation with no run is a result
    with nowhere to land. Everything after creation — phase, summary, the
    timestamps — is ingestion's, applied from `benchmark.observed`.
    """

    def __init__(self, db: AsyncSession, actor: str = "unknown", notifier: Notifier | None = None) -> None:
        self.db = db
        self.actor = actor
        self.repo = BenchmarkRepository(db)
        self.notifier: Notifier = DatabaseNotifier() if notifier is None else notifier

    # ----------------------------------------------------------------- harnesses

    def harnesses(self) -> list[BenchmarkHarnessSchema]:
        """The load generators and their knobs, so a client renders a form from the plane.

        The console would otherwise carry a second copy of the parameter table,
        and a second copy is a second answer to what AIPerf accepts.
        """
        return [
            BenchmarkHarnessSchema(
                name=harness.name,
                image=harness.image,
                description=harness.description,
                parameters=[
                    BenchmarkParameterSchema(
                        name=parameter.name,
                        type=parameter.type.__name__,
                        default=parameter.default,
                        description=parameter.description,
                    )
                    for parameter in harness.parameters
                ],
            )
            for harness in harnesses.HARNESSES.values()
        ]

    # ----------------------------------------------------------------- reads

    def schema(
        self,
        run: BenchmarkRun,
        deployment_uid: str,
        cluster_uid: str,
        operation_uid: str | None,
        recipe_uid: str | None,
    ) -> BenchmarkRunSchema:
        return BenchmarkRunSchema(
            id=run.uid,
            deployment_id=deployment_uid,
            cluster_id=cluster_uid,
            operation_id=operation_uid,
            harness=run.harness,
            harness_version=run.harness_version,
            image=run.image,
            parameters=run.parameters or {},
            requested_by=run.requested_by,
            reason=run.reason,
            provenance=BenchmarkProvenanceSchema(
                model=run.model,
                deployment_generation=run.deployment_generation,
                recipe_id=recipe_uid,
                recipe_version=run.recipe_version,
                engine_args=run.engine_args or {},
            ),
            phase=run.phase,
            summary=run.summary,
            artifacts_uri=run.artifacts_uri,
            error=run.error,
            created_at=run.created_at,
            updated_at=run.updated_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )

    def _row_schema(self, row: Row) -> BenchmarkRunSchema:
        return self.schema(row[0], row.deployment_uid, row.cluster_uid, row.operation_uid, row.recipe_uid)

    async def get(self, ref: str) -> BenchmarkRunSchema:
        row = await self.repo.get(ref)
        if row is None:
            raise NotFound(f"no benchmark run {ref!r}")
        return self._row_schema(row)

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        deployment: str | None = None,
        cluster: str | None = None,
        harness: str | None = None,
        phase: str | None = None,
        actor: str | None = None,
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
            since=since,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        return ListResponse(items=[self._row_schema(row) for row in kept], count=len(kept), next_cursor=next_cursor)

    # ----------------------------------------------------------------- writes

    async def _deployment(self, data: BenchmarkRunCreateRequest) -> Row:
        """The one deployment a reference names, or an error saying which way it failed.

        Same rule as an operation target: a name is unique per cluster and not
        above it, so an ambiguous one is refused rather than picked. Benchmarking
        an arbitrary one of two deployments that share a name would produce a
        number attributed to the wrong thing, which is worse than no number.
        """
        matches = await self.repo.deployment_targets(data.deployment)
        if not matches:
            raise NotFound(f"no deployment {data.deployment!r}")
        if len(matches) > 1:
            raise Invalid(
                f"deployment {data.deployment!r} matches {len(matches)} objects; use its id",
                {"deployment": data.deployment, "matches": sorted(row[0].uid for row in matches)},
            )
        return matches[0]

    async def create(self, data: BenchmarkRunCreateRequest) -> BenchmarkRunSchema:
        """Records what is to be measured, and asks the cluster to measure it.

        The provenance snapshot is taken here and not by the operator. What the
        deployment was running is a fact the plane already holds, and reading it
        at creation is what pins the run to a generation rather than to whatever
        the deployment happens to be by the time the job finishes.
        """
        harness = harnesses.resolve(data.harness)
        parameters = harnesses.normalise(harness, data.parameters or {})
        image = harnesses.resolve_image(harness, data.image)

        deployment, recipe, artifact = await self._deployment(data)
        if not deployment.enabled:
            raise Conflict(
                f"deployment {deployment.uid} is disabled; there is nothing serving to measure",
                {"deployment": deployment.uid},
            )

        model = served_model(recipe, artifact)
        if not parameters.get("tokenizer"):
            parameters["tokenizer"] = tokenizer_for(recipe, artifact)
        run_uid = new_uid(BENCHMARK_RUN)

        operation = Operation(
            uid=new_uid(OPERATION),
            cluster_id=deployment.cluster_id,
            type=OPERATION_TYPE,
            target_type="deployment",
            target_id=deployment.id,
            phase=PENDING,
            requested_by=self.actor,
            reason=data.reason,
            payload={
                "parameters": {
                    **harnesses.wire_parameters(harness, parameters, image),
                    "benchmark_id": run_uid,
                    "deployment_id": deployment.uid,
                    "model": model,
                }
            },
            deadline_at=data.deadline,
        )

        run = BenchmarkRun(
            uid=run_uid,
            deployment_id=deployment.id,
            cluster_id=deployment.cluster_id,
            harness=harness.name,
            image=image,
            parameters=parameters,
            requested_by=self.actor,
            reason=data.reason,
            model=model,
            deployment_generation=deployment.generation,
            recipe_id=recipe.id,
            recipe_version=recipe.version,
            engine_args=dict(recipe.engine_args or {}),
            phase=PENDING,
        )

        try:
            self.db.add(operation)
            await self.db.flush()
            run.operation_id = operation.id
            await self.repo.add(run)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("benchmark run could not be recorded") from exc

        cluster_uid = await self.repo.cluster_uid(deployment.cluster_id)
        if cluster_uid is not None:
            await self.notifier.changed(
                self.db,
                DeclaredChange(cluster_id=cluster_uid, kind=OPERATION_KIND, uid=operation.uid),
            )
        return await self.get(run_uid)


def tokenizer_for(recipe: Recipe, artifact: Artifact) -> str:
    """The repository AIPerf should load a tokenizer from, or nothing if we cannot say.

    Most authoritative first, and the served model name is nowhere in the list —
    it is what AIPerf falls back to on its own and it is the wrong answer: a
    deployment served as `qwen3.8-27b` is not a Hugging Face repository and the
    lookup fails before a single request is sent.

    An explicit `--tokenizer` wins, whether it reached the recipe hyphenated the
    way discovery writes engine arguments or bare the way a hand-written recipe
    does, because it is the engine being told which tokenizer it is using. The
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
    engine_args = recipe.engine_args or {}
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
