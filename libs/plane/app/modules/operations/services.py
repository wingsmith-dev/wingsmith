import json
import hashlib
import datetime as dt

import msgspec

from sqlalchemy import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import NODE
from app.core.ids import WORKLOAD
from app.core.ids import OPERATION
from app.core.ids import DEPLOYMENT
from app.core.ids import new_uid
from app.db.models import Cluster
from app.db.models import Operation
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
from app.modules.benchmarks.follow import follows_operation

from .schemas import OperationSchema
from .schemas import OperationTargetSchema
from .schemas import OperationCreateRequest
from .schemas import OperationSummarySchema
from .repository import TARGET_KINDS
from .repository import OperationRepository
from .repository import parse_target


TYPES: dict[str, tuple[str, str]] = {
    "node.drain": (NODE, "drain"),
    "node.cordon": (NODE, "cordon"),
    "node.uncordon": (NODE, "cordon"),
    "workload.scale": (WORKLOAD, "scale"),
    "workload.pause": (WORKLOAD, "pause"),
    "workload.reshape": (WORKLOAD, "reshape"),
    "workload.drain": (WORKLOAD, "drain"),
    "deployment.benchmark": (DEPLOYMENT, "benchmark"),
}
"""Every operation type something carries out, the id prefix its target must carry, and the wire kind.

The second element is what a `node.drain` targeting `dev_...` is checked
against: the type names the kind of thing it acts upon, so a mismatched prefix
is a category error at creation and not a failure discovered inside the cluster
an hour later. The third is the `operation.desired` kind the dispatcher will
send; `uncordon` shares `cordon` because the wire kind names the mechanism and
the parameters say which direction.
"""

NOT_SUPPORTED_YET = frozenset({"node.reboot", "device.reset", "cache.warm", "deployment.roll", "component.upgrade"})
"""Operation types that are on the roadmap and that no operator executes.

Refused at creation with a reason rather than recorded as `pending`, because a
pending operation nothing will ever pick up reads as a stuck cluster. They stay
named here so a listing can still filter on a type an older plane recorded.
"""

PLANE_EXECUTED = frozenset({"workload.scale", "workload.pause", "workload.reshape", "workload.drain"})
"""Operations the plane carries out itself, rather than handing to a cluster.

A workload spans clusters, so there is no single agent to dispatch these to. What
they actually change is declared state the plane owns — the per-cluster shares —
and the clusters converge on that afterwards through the ordinary `deployment.desired`
path. They are still operations rather than field writes because they are imperative
and they can fail: a scale-up that no cluster has the quota for fails here, with a
reason, instead of silently sitting in a spec nobody can satisfy.

They reach a terminal phase inside the request that created them. The plane's half is
synchronous — re-expansion either finds room or does not — and how far the clusters
have got with the result is a question the deployments already answer.
"""


def workload_parameters(type_name: str, parameters: dict) -> object | None:
    """Decodes a plane-executed operation's parameters into their typed struct, or refuses them.

    Done at creation so a malformed parameter block is a 400 on the request rather
    than an operation recorded and then failed.
    """
    from app.modules.workloads.services import OPERATION_PARAMETERS

    shape = OPERATION_PARAMETERS.get(type_name)
    if shape is None:
        return None
    try:
        return msgspec.convert(parameters or {}, shape)
    except msgspec.ValidationError as exc:
        raise Invalid(f"invalid parameters for {type_name}: {exc}", {"type": type_name}) from exc


def request_fingerprint(data: OperationCreateRequest, target_uid: str, actor: str) -> str:
    """A digest of everything a create request asks for, stored beside its idempotency key.

    A retry under the same key replays the operation only if this matches, so the same
    key sent with different parameters, a different reason or deadline, or by another
    principal is refused rather than answered with an operation it did not ask for. The
    target is the resolved id, so `nod_...` and `node:gpu-node-04` naming the same node
    are the same request.
    """
    request = {
        "actor": actor,
        "type": data.type,
        "target": target_uid,
        "parameters": data.parameters or {},
        "reason": data.reason,
        "deadline": data.deadline.isoformat() if data.deadline is not None else None,
    }
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


PENDING = "pending"
OPEN_PHASES = frozenset({"pending", "running"})
TERMINAL_PHASES = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
PHASES = OPEN_PHASES | TERMINAL_PHASES


def wire_kind(type_name: str) -> str:
    return TYPES[type_name][1]


class OperationService:
    """Writes the declared half of an operation exactly once, at creation.

    Everything after that — phase, result, error, started_at, finished_at — is
    ingestion's, applied from `operation.observed`. This service never writes
    those columns for a live operation, which is what keeps the two writers off
    each other's fields.
    """

    def __init__(self, db: AsyncSession, actor: str = "unknown", notifier: Notifier | None = None) -> None:
        self.db = db
        self.actor = actor
        self.repo = OperationRepository(db)
        self.notifier: Notifier = DatabaseNotifier() if notifier is None else notifier

    async def _announce(self, cluster_uid: str, uid: str) -> None:
        """Wakes whichever hub holds this cluster's session, after the row is committed.

        An operation is not declared state, but it reaches a cluster the same way
        one does, because there is no second channel between the two processes.
        The ordering is the same contract as elsewhere: the hub answers a hint by
        re-reading the row, so announcing inside the transaction would race the
        reader against a write that has not landed.

        Missing the wakeup costs latency and nothing else. Unlike declared state,
        what repairs it is not a resync — an operation is deliberately outside
        that snapshot — but the replay of open operations the session performs
        when the cluster next connects.
        """
        await self.notifier.changed(
            self.db,
            DeclaredChange(cluster_id=cluster_uid, kind=OPERATION_KIND, uid=uid),
        )

    def schema(self, operation: Operation, cluster_uid: str | None, target_uid: str) -> OperationSchema:
        payload = operation.payload or {}
        return OperationSchema(
            id=operation.uid,
            cluster_id=cluster_uid,
            type=operation.type,
            target=OperationTargetSchema(type=operation.target_type, id=target_uid),
            reason=operation.reason,
            requested_by=operation.requested_by,
            parameters=payload.get("parameters", {}),
            deadline_at=operation.deadline_at,
            created_at=operation.created_at,
            updated_at=operation.updated_at,
            phase=operation.phase,
            result=operation.result,
            error=operation.error,
            attempts=operation.attempts,
            cancel_requested_at=operation.cancel_requested_at,
            started_at=operation.started_at,
            finished_at=operation.finished_at,
        )

    def _row_schema(self, row: Row, target_uid: str) -> OperationSchema:
        return self.schema(row[0], row.cluster_uid, target_uid)

    async def _target_uid(self, operation: Operation) -> str:
        """Renders a stored row id back as the public id of whatever it points at.

        `target_id` is polymorphic and holds a primary key, so the prefix is
        recovered from `target_type` rather than from the value.
        """
        prefix = next((p for p, kind in TARGET_KINDS.items() if kind == operation.target_type), None)
        if prefix is None:
            return str(operation.target_id)
        return await self.repo.target_uid(prefix, operation.target_id) or str(operation.target_id)

    async def get(self, ref: str) -> OperationSchema:
        row = await self.repo.get(ref)
        if row is None:
            raise NotFound(f"no operation {ref!r}")
        return self._row_schema(row, await self._target_uid(row[0]))

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        kind: str | None = None,
        phase: str | None = None,
        target: str | None = None,
        actor: str | None = None,
        since: dt.datetime | None = None,
    ) -> ListResponse[OperationSchema]:
        if kind is not None and kind not in TYPES and kind not in NOT_SUPPORTED_YET:
            raise Invalid(f"unknown operation type {kind!r}", {"allowed": sorted(TYPES)})
        if phase is not None and phase not in PHASES:
            raise Invalid(f"unknown phase {phase!r}", {"allowed": sorted(PHASES)})
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            before_seq=decode_cursor(cursor),
            cluster=cluster,
            kind=kind,
            phase=phase,
            target=target,
            actor=actor,
            since=since,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = [self._row_schema(row, await self._target_uid(row[0])) for row in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def summary(self, cluster: str | None = None) -> OperationSummarySchema:
        """A read-time rollup of the collection by phase, no stored counters.

        Every phase gets a key so a caller reads a zero rather than a gap, and
        the two 24h fields are answered from the database's own clock through the
        repository's `func.now()` window.
        """
        counts = await self.repo.phase_counts(cluster)
        by_phase = {phase: counts.get(phase, 0) for phase in sorted(PHASES)}
        return OperationSummarySchema(
            total=sum(by_phase.values()),
            by_phase=by_phase,
            failed_24h=await self.repo.finished_since("failed", cluster),
            succeeded_24h=await self.repo.finished_since("succeeded", cluster),
        )

    async def _resolve(self, data: OperationCreateRequest, cluster: Cluster | None) -> Row:
        """The one row a target reference names, or an error saying which way it failed.

        A `kind:name` reference can match more than one row — a node name is
        unique inside a cluster and nowhere above it — and the request already
        carries the field that separates them, so a named cluster narrows the
        match before it is called ambiguous. What it must never do is pick one:
        draining an arbitrary machine of two that share a name is worse than
        refusing, so anything still plural after narrowing is a 400 that names
        the candidates.
        """
        matches = await self.repo.resolve_targets(data.target)
        if not matches:
            raise NotFound(f"no target {data.target!r}")
        if len(matches) > 1 and cluster is not None:
            matches = [row for row in matches if row.cluster_id == cluster.id]
        if len(matches) > 1:
            raise Invalid(
                f"target {data.target!r} matches {len(matches)} objects; use its id or name a cluster",
                {"target": data.target, "matches": sorted(row.uid for row in matches)},
            )
        if not matches:
            raise NotFound(f"no target {data.target!r} in cluster {data.cluster!r}")
        return matches[0]

    async def create(self, data: OperationCreateRequest, idempotency_key: str | None = None) -> OperationSchema:
        """Validates the target against the type, then records the intent.

        The phase written here is `pending` and nothing else: the plane records
        that somebody asked for this, and the cluster is what makes it happen.
        """
        if data.type in NOT_SUPPORTED_YET:
            raise Invalid(
                f"{data.type} is not supported yet: no operator carries it out",
                {"type": data.type, "supported": sorted(TYPES)},
            )
        if data.type not in TYPES:
            raise Invalid(f"unknown operation type {data.type!r}", {"allowed": sorted(TYPES)})
        prefix, _ = TYPES[data.type]
        parameters = workload_parameters(data.type, data.parameters)

        parsed = parse_target(data.target)
        if parsed.prefix != prefix:
            raise Invalid(
                f"{data.type} acts on {TARGET_KINDS[prefix]} targets, not {data.target!r}",
                {"type": data.type, "expected_kind": TARGET_KINDS[prefix], "target": data.target},
            )

        cluster = None
        if data.cluster is not None:
            cluster = await self.repo.cluster_by_ref(data.cluster)
            if cluster is None:
                raise NotFound(f"no cluster {data.cluster!r}")

        target = await self._resolve(data, cluster)

        fingerprint = request_fingerprint(data, target.uid, self.actor)
        if idempotency_key is not None:
            existing = await self.repo.by_idempotency_key(idempotency_key)
            if existing is not None:
                return await self._replay(existing, fingerprint)

        cluster_id = target.cluster_id
        if cluster is not None:
            if cluster_id is not None and cluster.id != cluster_id:
                raise Invalid(
                    f"target {data.target!r} does not belong to cluster {data.cluster!r}",
                    {"target": data.target, "cluster": data.cluster},
                )
            cluster_id = cluster.id
        if cluster_id is None and data.type not in PLANE_EXECUTED:
            raise Invalid(
                f"{data.type} needs a cluster: its target does not belong to one",
                {"type": data.type, "target": data.target},
            )

        operation = Operation(
            uid=new_uid(OPERATION),
            cluster_id=cluster_id,
            type=data.type,
            target_type=TARGET_KINDS[prefix],
            target_id=target.id,
            phase=PENDING,
            requested_by=self.actor,
            reason=data.reason,
            idempotency_key=idempotency_key,
            payload={"parameters": data.parameters, "request": fingerprint},
            deadline_at=data.deadline,
        )
        if data.type in PLANE_EXECUTED:
            await self.repo.add(operation)
            await self._execute(operation, target, parameters)
            return await self.get(operation.uid)

        try:
            await self.repo.add(operation)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            if idempotency_key is not None:
                replayed = await self.repo.by_idempotency_key(idempotency_key)
                if replayed is not None:
                    return await self._replay(replayed, fingerprint)
            raise Conflict("operation could not be recorded") from exc
        cluster_uid = await self.repo.cluster_uid(cluster_id)
        if cluster_uid is not None:
            await self._announce(cluster_uid, operation.uid)
        return await self.get(operation.uid)

    async def _replay(self, existing: Row, fingerprint: str) -> OperationSchema:
        """The operation an idempotency key already created, if this request is the same one.

        Anything else under the same key is a 409 that names no part of the stored request,
        since the key may have been used by another principal.
        """
        operation = existing[0]
        if (operation.payload or {}).get("request") != fingerprint:
            raise Conflict("idempotency key was already used for a different request")
        return self._row_schema(existing, await self._target_uid(operation))

    async def _execute(self, operation: Operation, target, parameters: object) -> None:
        """Carries out a plane-executed operation and closes it in one commit.

        The workload change is flushed, not committed, by the workload service; the
        operation's terminal phase is written beside it and the two commit together.
        A scale that succeeded but left no record of having been asked for, or a
        record of a scale that never landed, are both worse than either outcome on
        its own. Clusters are woken only after that commit.

        A refusal from placement — no cluster with the quota, nowhere the shape fits
        — is the operation failing, not the request being malformed. The change is
        rolled back and the operation alone is committed as `failed` with the reason,
        which is what a caller polling this is waiting for.
        """
        from app.modules.workloads.services import WorkloadService

        operation.started_at = dt.datetime.now(dt.UTC)
        service = WorkloadService(self.db, notifier=self.notifier)
        try:
            change = await service.change(operation.type, target.uid, parameters)
        except (Invalid, NotFound, Conflict) as exc:
            await self.db.rollback()
            await self.repo.add(operation)
            operation.phase = "failed"
            operation.error = str(exc)
            operation.finished_at = dt.datetime.now(dt.UTC)
            await self.db.commit()
            return
        operation.phase = "succeeded"
        operation.result = change.result
        operation.finished_at = dt.datetime.now(dt.UTC)
        await self.db.commit()
        await service.announce(change)

    async def cancel(self, ref: str) -> OperationSchema:
        """Records that the plane wants the operation stopped.

        `cancel_requested_at` is the plane's column and `phase` is ingestion's,
        which is what lets a `running` operation be cancelled at all: the request
        is a fact the cluster observes and answers with a terminal phase, not a
        phase the plane asserts on the cluster's behalf. The cluster is then told:
        an operation past `pending` has reached one, and the hint makes the hub
        re-read a row that now renders as `operation.cancel` rather than as the
        instruction it was. Announcing only in that branch is deliberate — the
        pending case was never sent, so there is nothing out there to withdraw. While the operation is
        still `pending` nothing has reported on it and no writer can be raced, so
        the phase moves to `cancelled` here as well and the request is recorded
        alongside it — one code path, and the pending case does not wait on a
        cluster that was never told.
        """
        row = await self.repo.get(ref)
        if row is None:
            raise NotFound(f"no operation {ref!r}")
        operation = row[0]
        if operation.phase in TERMINAL_PHASES:
            raise Conflict(
                f"operation {ref!r} already finished as {operation.phase!r}",
                {"phase": operation.phase},
            )
        if operation.cancel_requested_at is None:
            operation.cancel_requested_at = dt.datetime.now(dt.UTC)
        if operation.phase == PENDING:
            operation.phase = "cancelled"
            operation.finished_at = operation.cancel_requested_at
            # This path never reaches a cluster — the operation was still pending, so nothing was
            # ever told to do it — which means nothing downstream will report on whatever the
            # operation was carrying. A benchmark run is the case that has its own row to leave
            # open, and it is left open for ever unless the cancellation is carried across here.
            await self.db.execute(follows_operation(operation.uid, "cancelled"))
        await self.db.commit()
        if operation.phase != "cancelled":
            cluster_uid = await self.repo.cluster_uid(operation.cluster_id)
            if cluster_uid is not None:
                await self._announce(cluster_uid, operation.uid)
        return await self.get(operation.uid)
