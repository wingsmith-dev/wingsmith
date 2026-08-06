import datetime as dt

from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import or_
from sqlalchemy import and_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import NODE
from app.core.ids import POOL
from app.core.ids import DEVICE
from app.core.ids import CLUSTER
from app.core.ids import REPLICA
from app.core.ids import ARTIFACT
from app.core.ids import ENDPOINT
from app.core.ids import WORKLOAD
from app.core.ids import COMPONENT
from app.core.ids import OPERATION
from app.core.ids import DEPLOYMENT
from app.core.ids import TOPOLOGY_DOMAIN
from app.core.ids import is_uid
from app.db.models import Node
from app.db.models import Pool
from app.db.models import Event
from app.db.models import Device
from app.db.models import Cluster
from app.db.models import Replica
from app.db.models import Artifact
from app.db.models import Endpoint
from app.db.models import Workload
from app.db.models import Component
from app.db.models import Operation
from app.db.models import Deployment
from app.db.models import EndpointTarget
from app.db.models import TopologyDomain
from app.core.exceptions import Invalid


SUBJECT_TABLES = {
    CLUSTER: Cluster,
    WORKLOAD: Workload,
    NODE: Node,
    DEVICE: Device,
    TOPOLOGY_DOMAIN: TopologyDomain,
    POOL: Pool,
    ARTIFACT: Artifact,
    DEPLOYMENT: Deployment,
    REPLICA: Replica,
    ENDPOINT: Endpoint,
    OPERATION: Operation,
    COMPONENT: Component,
}


SUBJECT_DETAIL_KEYS = {
    DEPLOYMENT: "deployment_id",
    WORKLOAD: "workload_id",
}
"""Where an event records its subject's public id as well as its row id.

`subject_id` holds a primary key, so a subject that has been deleted can no
longer be found by the identifier a reader holds — the join that resolves
`dep_...` to a uuid returns nothing once the row is gone. For the kinds listed
here the public id is also written into `details`, and the filter falls back to
it, which is what makes "what happened to the deployment I just deleted"
answerable. It is the timeline of a thing that no longer exists that is worth
keeping; the row never was.
"""


def subject_subquery(ref: str):
    """Turns a public id into the row id `Event.subject_id` actually holds.

    Events are written with the subject's primary key, not its `nod_...` id, so
    a filter by public id has to go through the subject's own table. The prefix
    says which table that is, which is the one thing a prefixed identifier is
    unambiguously good for.
    """
    if not is_uid(ref):
        raise Invalid(f"subject {ref!r} is not an identifier", {"subject": ref})
    model = SUBJECT_TABLES.get(ref[:4])
    if model is None:
        raise Invalid(f"subject {ref!r} is not a kind events are written about", {"subject": ref})
    return select(model.id).where(model.uid == ref)


def workload_clause(ref: str):
    """Every event in one workload's story, including its children's.

    The workload's own events come first in the clause and first in life: they are
    what the plane did before any cluster had been chosen — the request landing,
    placement running, and where it landed or why it landed nowhere. Everything
    after that happens to a deployment, a replica, or the weights those replicas
    load, and a workload that never got as far as a deployment has only the first
    kind. Filtering on the children alone returned an empty timeline for exactly
    the workload that most needed one.

    Four generations, in the order a reader meets them. The deployments are the
    workload's share of each cluster; the replicas are what those deployments
    run; and the artifact is the one thing outside the workload's own tree that
    can hold all of it — a deployment waiting on weights that are not resident is
    waiting on an event filed against the artifact.

    The artifact leg is scoped to the clusters this workload is deployed on. The
    same weights may back several workloads, and a download on a cluster this
    workload never reached is somebody else's timeline. It is also bounded to
    events since the workload was created: the weights outlive every workload that
    loads them, and without the bound a workload minutes old showed the downloads
    an earlier workload waited on weeks ago.
    """
    deployments = select(Deployment.id).join(Workload, Workload.id == Deployment.workload_id).where(Workload.uid == ref)
    replicas = select(Replica.id).where(Replica.deployment_id.in_(deployments))
    artifact = select(Artifact.id).join(Workload, Workload.artifact_id == Artifact.id).where(Workload.uid == ref)
    clusters = (
        select(Deployment.cluster_id).join(Workload, Workload.id == Deployment.workload_id).where(Workload.uid == ref)
    )
    created = select(Workload.created_at).where(Workload.uid == ref).scalar_subquery()
    return or_(
        Event.subject_id.in_(subject_subquery(ref)),
        Event.subject_id.in_(deployments),
        Event.subject_id.in_(replicas),
        Event.details["workload_id"].astext == ref,
        and_(
            Event.subject_type == "artifact",
            Event.subject_id.in_(artifact),
            Event.cluster_id.in_(clusters),
            Event.occurred_at >= created,
        ),
    )


def endpoint_clause(ref: str):
    """Every event in one endpoint's story, which is its targets' stories.

    Nothing writes an event whose subject is an endpoint, for the same reason
    nothing writes one about a workload: an endpoint is a name and a weighting,
    and everything that can go wrong with it goes wrong to a deployment it
    points at or to a replica that deployment runs. Filtering on the endpoint
    alone returned an empty timeline for an endpoint that was visibly failing.

    Only the deployments its targets name, and only their replicas. The
    workload behind a target is deliberately not walked: a target is a share of
    traffic sent to one deployment, and the other clusters the same workload
    landed on are not this endpoint's story even though they serve the same
    weights. The endpoint's own subject stays in the clause so that a writer
    filing `endpoint.*` later needs no change here.
    """
    deployments = (
        select(EndpointTarget.deployment_id)
        .join(Endpoint, Endpoint.id == EndpointTarget.endpoint_id)
        .where(Endpoint.uid == ref)
    )
    replicas = select(Replica.id).where(Replica.deployment_id.in_(deployments))
    return or_(
        Event.subject_id.in_(subject_subquery(ref)),
        Event.subject_id.in_(deployments),
        Event.subject_id.in_(replicas),
    )


def subject_clause(ref: str):
    """Matches events about one subject, whether or not the subject still exists."""
    if not is_uid(ref):
        raise Invalid(f"subject {ref!r} is not an identifier", {"subject": ref})
    if ref[:4] == WORKLOAD:
        return workload_clause(ref)
    if ref[:4] == ENDPOINT:
        return endpoint_clause(ref)
    matched = Event.subject_id.in_(subject_subquery(ref))
    key = SUBJECT_DETAIL_KEYS.get(ref[:4])
    if key is None:
        return matched
    return or_(matched, Event.details[key].astext == ref)


class EventRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return select(Event, Cluster.uid.label("cluster_uid")).outerjoin(Cluster, Event.cluster_id == Cluster.id)

    def get_query(self, ref: str) -> Select:
        """Resolves an event the way every other module resolves a reference.

        An event has no name, so `evt_...` is the only reference there is: the
        row's primary key is machinery and has never been a public identifier.
        """
        return self._base().where(Event.uid == ref)

    def list_query(
        self,
        limit: int,
        before_seq: int | None = None,
        cluster: str | None = None,
        subject: str | None = None,
        kind: str | None = None,
        severity: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
    ) -> Select:
        """Newest first: the page walks `seq` downwards rather than upwards.

        Ordering is on `seq` and not on `occurred_at`. `occurred_at` is written
        by the cluster, so it is neither unique nor monotonic across clusters,
        and a cursor on it would skip or repeat rows whenever two events shared a
        timestamp or a clock stepped backwards. `seq` is assigned on insert, is
        unique, and is the same key `app/core/pagination` already encodes — only
        the direction of the comparison changes, which is why this needs no
        cursor format of its own.
        """
        query = self._base().order_by(Event.seq.desc()).limit(limit + 1)
        if before_seq is not None:
            query = query.where(Event.seq < before_seq)
        if cluster is not None:
            column = Cluster.uid if is_uid(cluster) else Cluster.name
            query = query.where(column == cluster)
        if subject is not None:
            query = query.where(subject_clause(subject))
        if kind is not None:
            query = query.where(Event.type == kind)
        if severity is not None:
            query = query.where(Event.severity == severity)
        if since is not None:
            query = query.where(Event.occurred_at >= since)
        if until is not None:
            query = query.where(Event.occurred_at < until)
        return query

    async def get(self, ref) -> Row | None:
        result = await self.db.execute(self.get_query(ref))
        return result.first()

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.all())
