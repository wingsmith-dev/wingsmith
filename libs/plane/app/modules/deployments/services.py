from __future__ import annotations

import datetime as dt

from msgspec import UNSET
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Deployment
from app.db.notify import DEPLOYMENT as DEPLOYMENT_KIND
from app.db.notify import Notifier
from app.db.notify import DeclaredChange
from app.db.notify import DatabaseNotifier
from app.ingestion import statements
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import DeploymentSchema
from .schemas import DeploymentStatusSchema
from .schemas import DeploymentUpdateRequest
from .schemas import DeploymentAuthorityRequest
from .schemas import DeploymentRoleObservedSchema
from .schemas import DeploymentReplicaRollupSchema
from .repository import DeploymentRepository
from .repository import bump_generation


PHASES = frozenset({"pending", "admitted", "placed", "running", "succeeded", "failed"})
"""Admission, which is per-cluster and therefore read off the deployment.

The same six values the workload used to carry, in the same order; what moved is
which row answers them. A workload running in one region and queued on quota in
another has no single phase, which is why there is no longer one.
"""

OBSERVED = "observed"
MANAGED = "managed"
AUTHORITIES = frozenset({OBSERVED, MANAGED})
"""Who owns a deployment, and the only two answers there are.

Binary on purpose. A deployment with some fields enforced and others left to drift
cannot be reasoned about at the moment somebody needs to, so the object answers in
one word (ADR 0009).
"""

EMPTY = DeploymentReplicaRollupSchema(
    total=0,
    ready=0,
    running=0,
    pending=0,
    failed=0,
    at_current_generation=0,
)


def _revision(deployment: Deployment) -> str:
    """The value `If-Match` is checked against.

    Derived from the deployment row alone, which it could not be while a
    deployment was two rows: a patch that moved priority used to touch the
    workload and leave `demand_deployment.updated_at` where it was. Nothing this
    module writes reaches the workload any more, so the one stamp is the whole
    answer; a workload-level change is versioned by the workload's own revision.
    """
    return str(int((deployment.updated_at or deployment.created_at).timestamp() * 1_000_000))


def _status(row) -> DeploymentStatusSchema | None:
    """The observed half, or nothing if no cluster has reported on it yet."""
    if row is None:
        return None
    return DeploymentStatusSchema(
        observed_generation=row.observed_generation,
        phase=row.phase,
        reason=row.reason,
        message=row.message,
        desired_replicas=row.desired_replicas,
        ready_replicas=row.ready_replicas,
        updated_replicas=row.updated_replicas,
        unavailable_replicas=row.unavailable_replicas,
        last_error_code=row.last_error_code,
        last_error_message=row.last_error_message,
        last_error_at=row.last_error_at,
        observed_at=row.observed_at,
        roles=observed_roles(row.roles),
    )


def observed_roles(roles) -> list[DeploymentRoleObservedSchema] | None:
    """The per-role counts, or nothing at all when the cluster reported none.

    The empty case answers as `None` and not as `[]`, because the column stores an
    aggregated deployment's absence of roles as an empty array and the API must not
    turn that into a claim that this deployment's roles are known to be zero.

    Public, and imported by `workloads/services.py`, because `DeploymentStatusSchema`
    is built in two places and a second private copy of this rule is how the two
    come to disagree about what an empty column means. The duplicate `_status` is
    the older problem and is not this change's to fix; sharing the part that is new
    at least stops it growing.
    """
    if not roles:
        return None
    return [
        DeploymentRoleObservedSchema(role=role["role"], desired=role["desired"], ready=role["ready"]) for role in roles
    ]


def _rollup(row: dict | None) -> DeploymentReplicaRollupSchema:
    if not row:
        return EMPTY
    return DeploymentReplicaRollupSchema(
        total=row.get("total", 0),
        ready=row.get("ready", 0),
        running=row.get("running", 0),
        pending=row.get("pending", 0),
        failed=row.get("failed", 0),
        at_current_generation=row.get("at_current_generation", 0),
    )


class DeploymentService:
    """Reads deployments, and writes the two things that are per-cluster decisions.

    A deployment is not created here and not deleted here: placement creates it
    when it expands a workload, and removes it when the workload contracts. What
    is left is reading, taking or releasing management of one cluster's copy, and
    enabling or disabling it — none of which is a statement about what was asked
    for, all of which are statements about this one cluster.
    """

    def __init__(self, db: AsyncSession, notifier: Notifier | None = None) -> None:
        self.db = db
        self.repo = DeploymentRepository(db)
        self.notifier: Notifier = DatabaseNotifier() if notifier is None else notifier

    async def _record(
        self,
        deployment: Deployment,
        event: str,
        message: str,
        details: dict[str, str] | None = None,
        severity: str = "info",
    ) -> None:
        """Files what the plane just did to a deployment, in the caller's transaction.

        Before the caller's `commit()` on purpose. The event and the write it
        describes are one fact, and committing them separately would allow the
        pair where a deployment exists and nothing records it being asked for —
        the exact gap that made a deployment's first minutes unreadable.

        This is a declared-state module writing to an observed table, which the
        layout otherwise forbids. It is allowed here because the event is not an
        observation of a cluster: it is the record of an intent this service is
        the only writer of, and `ops_event` is the one timeline a reader should
        have to consult. The distinction is kept visible in the event type —
        `deployment.authority_changed` is something a human did,
        `deployment.placed` is something a cluster concluded.
        """
        await self.db.execute(
            statements.deployment_lifecycle(
                deployment.cluster_id,
                deployment.id,
                event,
                message,
                {"deployment_id": deployment.uid, "name": deployment.name, **(details or {})},
                dt.datetime.now(dt.UTC),
                severity,
            )
        )

    async def _announce(self, cluster_uid: str, uid: str, generation: int) -> None:
        """Wakes whichever hub holds this cluster's stream, after the write is committed.

        Every caller of this is on the far side of its own `commit()`, and that
        ordering is the contract: the hub answers the hint by re-reading the row,
        so announcing first would race the reader against a transaction that has
        not landed. Missing the wakeup entirely costs latency and nothing else —
        the next full resync carries the same state — which is why this never
        stands between the caller and its response.
        """
        await self.notifier.changed(
            self.db,
            DeclaredChange(cluster_id=cluster_uid, kind=DEPLOYMENT_KIND, uid=uid, generation=generation),
        )

    def schema(
        self,
        deployment: Deployment,
        workload_uid: str,
        workload_name: str,
        cluster_uid: str,
        replicas: DeploymentReplicaRollupSchema,
        status: DeploymentStatusSchema | None = None,
    ) -> DeploymentSchema:
        return DeploymentSchema(
            id=deployment.uid,
            name=deployment.name,
            workload_id=workload_uid,
            workload_name=workload_name,
            cluster_id=cluster_uid,
            min_replicas=deployment.min_replicas,
            max_replicas=deployment.max_replicas,
            desired_replicas=deployment.desired_replicas,
            admission_phase=deployment.admission_phase,
            admission_reason=deployment.admission_reason,
            queued_at=deployment.queued_at,
            admitted_at=deployment.admitted_at,
            placed_at=deployment.placed_at,
            generation=deployment.generation,
            enabled=deployment.enabled,
            created_at=deployment.created_at,
            updated_at=deployment.updated_at,
            revision=_revision(deployment),
            adopted_workload_id=deployment.adopted_workload_uid,
            adopted_fingerprint=deployment.adopted_fingerprint,
            authority=deployment.authority,
            replicas=replicas,
            status=status,
        )

    async def _one(self, ref: str, cluster: str | None = None, lock: bool = False):
        row = await self.repo.get(ref, cluster, lock=lock)
        if row is None:
            raise NotFound(f"no deployment {ref!r}")
        return row

    def _row_schema(self, row, replicas: DeploymentReplicaRollupSchema) -> DeploymentSchema:
        return self.schema(
            deployment=row[0],
            workload_uid=row.workload_uid,
            workload_name=row.workload_name,
            cluster_uid=row.cluster_uid,
            replicas=replicas,
            status=_status(row.DeploymentStatus),
        )

    async def _render(self, row, replicas: DeploymentReplicaRollupSchema | None = None) -> DeploymentSchema:
        deployment = row[0]
        if replicas is None:
            rollup = await self.repo.replicas([deployment.id])
            replicas = _rollup(rollup.get(deployment.id))
        return self._row_schema(row, replicas)

    async def get(self, ref: str, cluster: str | None = None) -> DeploymentSchema:
        return await self._render(await self._one(ref, cluster))

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        workload: str | None = None,
        project: str | None = None,
        phase: str | None = None,
    ) -> ListResponse[DeploymentSchema]:
        if phase is not None and phase not in PHASES:
            raise Invalid(f"unknown phase {phase!r}", {"allowed": sorted(PHASES)})
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            cluster=cluster,
            workload=workload,
            project=project,
            phase=phase,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        rollup = await self.repo.replicas([r[0].id for r in kept])
        items = [self._row_schema(r, _rollup(rollup.get(r[0].id))) for r in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def set_authority(self, ref: str, data: DeploymentAuthorityRequest) -> DeploymentSchema:
        """Takes management of a deployment, or gives it back.

        The two directions are not symmetric and are not written as though they
        were. `managed` is the direction that can write to a cluster, so an adopted
        deployment has to prove the caller was looking at what is running now: the
        fingerprint they send is compared against the current sighting, not against
        `adopted_fingerprint`, which is a record of what was true when it was
        promoted. `observed` is the plane letting go and needs nothing.

        A deployment the plane created itself has no sighting and no fingerprint to
        check, and taking management of it is not a change of ownership — it is the
        ownership it was created with, made explicit (ADR 0009).
        """
        if data.authority not in AUTHORITIES:
            raise Invalid(f"unknown authority {data.authority!r}", {"allowed": sorted(AUTHORITIES)})

        row = await self._one(ref, lock=True)
        deployment = row[0]
        if deployment.authority == data.authority:
            return await self._render(row)

        if data.authority == MANAGED and deployment.adopted_workload_uid:
            live = await self.repo.sighting_fingerprint(deployment.cluster_id, deployment.adopted_workload_uid)
            if live is None:
                raise Conflict(
                    "this cluster is no longer reporting the adopted workload, so there is nothing "
                    "to check the digest against",
                    {"workload": deployment.adopted_workload_uid},
                )
            if not data.fingerprint:
                raise Invalid(
                    "taking management of an adopted workload needs the fingerprint you were shown",
                    {"observed": live},
                )
            if data.fingerprint != live:
                raise Conflict(
                    "the workload changed since it was read; re-read it and try again",
                    {"observed": live, "sent": data.fingerprint},
                )
            deployment.adopted_fingerprint = live

        deployment.authority = data.authority
        await bump_generation(self.db, deployment)
        await self._record(
            deployment,
            "authority_changed",
            f"{deployment.name} is now {data.authority}",
            {"authority": data.authority, "generation": str(deployment.generation)},
        )
        await self.db.commit()
        await self._announce(row.cluster_uid, deployment.uid, deployment.generation)
        return await self.get(deployment.uid)

    async def update(self, ref: str, data: DeploymentUpdateRequest, if_match: str | None = None) -> DeploymentSchema:
        """Enables or disables one cluster's copy, and nothing else.

        The rest of what a deployment used to accept is declared on the workload
        now, once, globally; scale is derived by placement from the workload total
        rather than asserted per cluster. Disabling is the one per-cluster
        statement left, and it is a declaration rather than an operation because it
        takes effect the moment the cluster reads it.
        """
        row = await self._one(ref, lock=True)
        deployment = row[0]
        revision = _revision(deployment)
        if if_match is not None and if_match != revision:
            raise Conflict("deployment changed since it was read", {"revision": revision})

        fields = {k: v for k, v in data.to_dict().items() if v is not UNSET}
        for key, value in fields.items():
            setattr(deployment, key, value)

        try:
            await self._record(
                deployment,
                "updated",
                f"{deployment.name} was {'enabled' if deployment.enabled else 'disabled'}"
                if "enabled" in fields
                else f"{deployment.name} was updated",
                {
                    "fields": ", ".join(sorted(data.to_dict(exclude_unset=True))),
                    "enabled": str(deployment.enabled).lower(),
                    "generation": str(deployment.generation),
                },
            )
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("deployment conflicts with an existing one") from exc
        await self._announce(row.cluster_uid, deployment.uid, deployment.generation)
        return await self.get(deployment.uid)
