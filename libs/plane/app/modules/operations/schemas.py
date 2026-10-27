import datetime as dt

from app.core.schemas import Struct


class OperationTargetSchema(Struct):
    """What the operation acts upon, as a kind and a public id.

    The kind is carried explicitly, not left to be read off the prefix,
    because a client that has to parse ids to know what it is looking at is a
    client that will eventually parse one wrong.
    """

    type: str
    id: str


class OperationSchema(Struct):
    """An imperative action with a lifecycle, not a verb that returned 200.

    The declared half (type, target, reason, actor, deadline) is written once
    by the API at creation, with `cancel_requested_at` as the one field the plane
    may write afterwards. The observed half (phase, result, error, attempts,
    and the two timestamps) is written by ingestion from `operation.observed`
    thereafter. The two writers do not share a field after creation, the same way
    `Cluster.phase` and `ClusterSyncState` are split.
    """

    id: str
    cluster_id: str | None
    type: str
    target: OperationTargetSchema
    reason: str | None
    requested_by: str
    parameters: dict
    deadline_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime | None
    phase: str
    result: dict | None
    error: str | None
    attempts: int
    cancel_requested_at: dt.datetime | None
    started_at: dt.datetime | None
    finished_at: dt.datetime | None


class OperationSummarySchema(Struct):
    """A rollup of the operations collection by phase, computed at read time.

    `by_phase` carries a count for every phase the fleet currently holds, so a
    console can render the mix without paging the collection. `total` is their
    sum. The two `_24h` fields are the terminal phases that matter for an
    at-a-glance health read (how many finished badly or well in the last day),
    and their window is closed by the database's own `now()`, not the plane's
    wall clock, so the answer is the same regardless of which process asked.
    """

    total: int
    by_phase: dict[str, int]
    failed_24h: int
    succeeded_24h: int


class OperationCreateRequest(Struct):
    """`{ "type": ..., "target": ... }`, never a verb on the target's own path.

    `target` is either a prefixed id (`nod_01hq0v...`) or a kind-qualified name
    (`node:gpu-node-04`). Both forms say what kind of thing they name, which is
    what lets a `node.drain` pointed at a device be refused at creation instead
    of inside the cluster an hour later; a bare name says nothing and is
    refused.

    `cluster` is accepted only for targets that do not carry a cluster of their
    own; for everything else the cluster is derived from the target, because two
    ways to say which cluster is two ways for them to disagree. It does one
    extra job for a qualified name: names are unique per cluster, so naming one
    is how a name that matches twice is narrowed to the one that was meant.
    """

    type: str
    target: str
    reason: str | None = None
    deadline: dt.datetime | None = None
    parameters: dict = {}
    cluster: str | None = None
