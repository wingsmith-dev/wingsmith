"""Operations, read out of Postgres and shaped into the frame each is owed.

Beside `desired.py`, not inside it, and the distance is the point. That
module renders the resync snapshot, and everything a snapshot carries is
prunable by definition: what is absent from a complete snapshot is what the
cluster stops running. An operation is the third kind of thing, imperative, long-running, and usually half-done by the time a session drops,
so folding it into that stream would make a drain that is already evicting pods
cancellable by a reconnect. Keeping the renderer in its own file means there is
no query shape in which an operation can accidentally end up inside
`stream_desired`.

Read-only, like its neighbour: nothing here opens a transaction or writes a row.
The wire kind and the open phases come from `app.modules.operations` instead of
being restated here, because a second copy of that table is a second answer to
what `node.uncordon` puts on the wire.

Two frames, not one. An operation the plane has asked to stop renders as
`operation.cancel` and never again as the instruction, which is what stops a
reconnect from re-sending a drain the caller already withdrew.
"""

from collections.abc import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Cluster
from app.db.models import Operation
from app.hub.messages import OperationCancel
from app.hub.messages import OperationTarget
from app.hub.messages import OperationDesired
from app.ingestion.desired import format_ts
from app.ingestion.statements import cluster_ref_clause
from app.modules.operations.services import TYPES
from app.modules.operations.services import OPEN_PHASES
from app.modules.operations.services import wire_kind
from app.modules.operations.repository import TARGET_KINDS
from app.modules.operations.repository import TARGET_TABLES


DESIRED = "operation.desired"
CANCEL = "operation.cancel"

CORDON_DIRECTION = {"node.cordon": "true", "node.uncordon": "false"}
"""Which way the shared `cordon` kind is being asked to go.

`node.cordon` and `node.uncordon` are one mechanism on the wire, so the type name
survives only as this parameter; without it the operator would have no way to
tell the two apart.
"""


async def operation_desired(db: AsyncSession, uid: str) -> tuple[str, OperationDesired | OperationCancel] | None:
    """One operation, rendered for the cluster it was created against.

    `None` when there is no such operation, when it belongs to no cluster, or
    when its type is one this seam does not know: an operation with nowhere to
    go is not an error here, it is simply nothing to send.
    """
    row = (
        await db.execute(
            select(Operation, Cluster.uid).join(Cluster, Operation.cluster_id == Cluster.id).where(Operation.uid == uid)
        )
    ).first()
    if row is None:
        return None
    return await _render(db, row[0], row[1])


async def open_operations(
    db: AsyncSession, cluster_uid: str
) -> AsyncIterator[tuple[str, OperationDesired | OperationCancel]]:
    """Every operation for one cluster that has not reached a terminal phase.

    Oldest first, because an operator that was offline through several of them
    should work them in the order they were asked for.
    """
    rows = (
        await db.execute(
            select(Operation)
            .join(Cluster, Operation.cluster_id == Cluster.id)
            .where(cluster_ref_clause(cluster_uid), Operation.phase.in_(OPEN_PHASES))
            .order_by(Operation.seq)
        )
    ).scalars()
    for operation in rows:
        rendered = await _render(db, operation, cluster_uid)
        if rendered is not None:
            yield rendered


async def _render(
    db: AsyncSession, operation: Operation, cluster_uid: str
) -> tuple[str, OperationDesired | OperationCancel] | None:
    """The frame this operation is currently owed, which is not always the instruction.

    An operation the plane has asked to stop renders as `operation.cancel` and
    never again as `operation.desired`. One row therefore has two renderings and
    this is where they part, which keeps every caller, the live hint, the replay
    of open operations on reconnect, sending the right one without knowing there
    are two. Re-sending the instruction to a cluster that was told to abandon it
    is the failure this ordering exists to prevent, and a reconnect is exactly
    when it would happen.
    """
    if operation.type not in TYPES:
        return None
    target_uid = await _target_uid(db, operation)
    if target_uid is None:
        return None
    if operation.cancel_requested_at is not None:
        return CANCEL, OperationCancel(
            operation_id=operation.uid,
            cluster_id=cluster_uid,
            reason=operation.reason or "",
            requested_by=operation.requested_by,
            requested_at=format_ts(operation.cancel_requested_at),
        )
    return DESIRED, OperationDesired(
        operation_id=operation.uid,
        cluster_id=cluster_uid,
        kind=wire_kind(operation.type),
        target=OperationTarget(kind=operation.target_type, id=target_uid),
        reason=operation.reason or "",
        requested_by=operation.requested_by,
        deadline=format_ts(operation.deadline_at) if operation.deadline_at is not None else "",
        parameters=_parameters(operation),
        created_at=format_ts(operation.created_at),
    )


def _parameters(operation: Operation) -> dict[str, str]:
    """What the caller asked for, plus what the wire kind cannot express on its own.

    The injected direction is written first so a caller who supplied a `cordon`
    parameter of their own wins: they named the mechanism explicitly, and this is
    only inferring it from the type.
    """
    parameters: dict[str, str] = {}
    direction = CORDON_DIRECTION.get(operation.type)
    if direction is not None:
        parameters["cordon"] = direction
    for key, value in ((operation.payload or {}).get("parameters") or {}).items():
        parameters[key] = str(value)
    return parameters


async def _target_uid(db: AsyncSession, operation: Operation) -> str | None:
    """The public id of whatever the operation points at.

    `target_id` is a primary key in one of six tables and `target_type` is the
    only thing that says which, so the table is recovered from the kind rather
    than from the value.
    """
    prefix = next((p for p, kind in TARGET_KINDS.items() if kind == operation.target_type), None)
    model = TARGET_TABLES.get(prefix) if prefix is not None else None
    if model is None:
        return None
    return await db.scalar(select(model.uid).where(model.id == operation.target_id))
