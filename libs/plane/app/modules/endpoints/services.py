from __future__ import annotations

import datetime as dt

from uuid import UUID

from msgspec import UNSET
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import ENDPOINT
from app.core.ids import new_uid
from app.db.models import Endpoint
from app.db.models import EndpointTarget
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor
from app.modules.quotas.services import declared_limits

from .schemas import EndpointSchema
from .schemas import ResolvedTableSchema
from .schemas import EndpointTargetSchema
from .schemas import EndpointCreateRequest
from .schemas import EndpointTargetRequest
from .schemas import EndpointUpdateRequest
from .schemas import ResolvedEndpointSchema
from .schemas import ResolvedUpstreamSchema
from .repository import EndpointRepository


VISIBILITIES = frozenset({"private", "public"})
STRATEGIES = frozenset({"weighted", "failover", "latency"})
ROLES = frozenset({"primary", "canary", "fallback"})


def check_targets(targets: list[EndpointTargetRequest]) -> None:
    """Rejects a target set that could not route.

    Two deployments cannot appear twice — the table's unique constraint says so
    and an integrity error at commit time names neither of them — and a set of
    enabled targets whose weights sum to zero is an endpoint that accepts traffic
    and sends it nowhere, which reads as an outage rather than as configuration.
    """
    seen: set[str] = set()
    live = 0
    for target in targets:
        if target.weight < 0:
            raise Invalid("a target weight cannot be negative", {"deployment": target.deployment})
        if target.role not in ROLES:
            raise Invalid(f"unknown role {target.role!r}", {"allowed": sorted(ROLES)})
        if target.deployment in seen:
            raise Invalid(f"deployment {target.deployment!r} appears twice", {"deployment": target.deployment})
        seen.add(target.deployment)
        if target.enabled:
            live += target.weight
    if targets and not live:
        raise Invalid("no enabled target carries any weight")


def check_shape(visibility: str, routing_strategy: str) -> None:
    if visibility not in VISIBILITIES:
        raise Invalid(f"unknown visibility {visibility!r}", {"allowed": sorted(VISIBILITIES)})
    if routing_strategy not in STRATEGIES:
        raise Invalid(f"unknown routing strategy {routing_strategy!r}", {"allowed": sorted(STRATEGIES)})


def _revision(endpoint: Endpoint) -> str:
    """The value `If-Match` is checked against, derived from the last write."""
    stamp = endpoint.updated_at or endpoint.created_at
    return str(int(stamp.timestamp() * 1_000_000))


def _stamp(row) -> dt.datetime | None:
    """When a row last moved, for the revision the whole table is versioned by."""
    if row is None:
        return None
    return row.updated_at or row.created_at


def _revision_of(stamps: list[dt.datetime | None]) -> str:
    """The newest stamp a resolution was built from, and how many there were.

    An empty table still needs a value a caller can send back as `If-None-Match`,
    and it needs the value to change the moment the table stops being empty —
    which it does, because the first row that appears carries a stamp.

    The second term is what makes the value sensitive to a *deletion*. A create
    stamps a new row and an update fires `onupdate`, so both push the maximum
    forward; a delete only removes a stamp, and unless that stamp happened to be
    the maximum across the whole fleet the value does not move. The gateway would
    then sit behind a `304` holding a route, or a rate limit, that no longer
    exists — and the asymmetry is what makes it dangerous, because create and
    update work and only delete silently does not. The count moves on any
    disappearance; the maximum still catches a simultaneous add and delete,
    because the added row is freshly stamped. The value stays opaque to its
    readers, which compare it as a string.
    """
    live = [stamp for stamp in stamps if stamp is not None]
    if not live:
        return "0-0"
    return f"{int(max(live).timestamp() * 1_000_000)}-{len(live)}"


class EndpointService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = EndpointRepository(db)

    def schema(self, endpoint: Endpoint, project_uid: str, uids: dict[UUID, str]) -> EndpointSchema:
        return EndpointSchema(
            id=endpoint.uid,
            name=endpoint.name,
            display_name=endpoint.display_name,
            project_id=project_uid,
            visibility=endpoint.visibility,
            routing_strategy=endpoint.routing_strategy,
            enabled=endpoint.enabled,
            targets=[
                EndpointTargetSchema(
                    deployment_id=uids[t.deployment_id],
                    weight=t.weight,
                    role=t.role,
                    enabled=t.enabled,
                )
                for t in endpoint.targets
                if t.deployment_id in uids
            ],
            failover_order=[uids[d] for d in endpoint.failover_order if d in uids],
            labels=endpoint.labels,
            created_at=endpoint.created_at,
            updated_at=endpoint.updated_at,
            revision=_revision(endpoint),
        )

    def _referenced(self, endpoints: list[Endpoint]) -> list[UUID]:
        ids: list[UUID] = []
        for endpoint in endpoints:
            ids.extend(t.deployment_id for t in endpoint.targets)
            ids.extend(endpoint.failover_order)
        return ids

    async def _one(self, ref: str):
        row = await self.repo.get(ref)
        if row is None:
            raise NotFound(f"no endpoint {ref!r}")
        return row

    async def _render(self, endpoint: Endpoint, project_uid: str) -> EndpointSchema:
        uids = await self.repo.deployment_uids(self._referenced([endpoint]))
        return self.schema(endpoint, project_uid, uids)

    async def get(self, ref: str) -> EndpointSchema:
        row = await self._one(ref)
        return await self._render(row[0], row.project_uid)

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        project: str | None = None,
        artifact: str | None = None,
        search: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> ListResponse[EndpointSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            project=project,
            artifact=artifact,
            search=search,
            labels=labels,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        uids = await self.repo.deployment_uids(self._referenced([r[0] for r in kept]))
        items = [self.schema(r[0], r.project_uid, uids) for r in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def _resolve(
        self, targets: list[EndpointTargetRequest], failover: list[str]
    ) -> tuple[list[EndpointTarget], list[UUID]]:
        """Turns target and failover references into rows and an ordered id array.

        The failover order is resolved against the target set rather than against
        every deployment there is: an endpoint that fails over to something it
        does not route to is a routing table with a hole in it.
        """
        refs = [t.deployment for t in targets]
        found = await self.repo.deployments_by_ref(refs)
        missing = [ref for ref in refs if ref not in found]
        if missing:
            raise NotFound("unknown deployment", {"missing": sorted(set(missing))})

        rows = [
            EndpointTarget(
                deployment_id=found[t.deployment].id,
                weight=t.weight,
                role=t.role,
                enabled=t.enabled,
            )
            for t in targets
        ]

        order: list[UUID] = []
        for ref in failover:
            deployment = found.get(ref)
            if deployment is None:
                raise Invalid(
                    f"failover names {ref!r}, which is not a target of this endpoint",
                    {"deployment": ref},
                )
            if deployment.id in order:
                raise Invalid(f"failover names {ref!r} twice", {"deployment": ref})
            order.append(deployment.id)
        return rows, order

    async def create(self, data: EndpointCreateRequest) -> EndpointSchema:
        check_shape(data.visibility, data.routing_strategy)
        check_targets(data.targets)
        project = await self.repo.project_by_ref(data.project)
        if project is None:
            raise NotFound(f"no project {data.project!r}")
        targets, failover_order = await self._resolve(data.targets, data.failover_order)

        endpoint = Endpoint(
            uid=new_uid(ENDPOINT),
            project_id=project.id,
            name=data.name,
            display_name=data.display_name,
            visibility=data.visibility,
            routing_strategy=data.routing_strategy,
            failover_order=failover_order,
            enabled=data.enabled,
            labels=data.labels,
            targets=targets,
        )
        try:
            await self.repo.add(endpoint)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"endpoint {data.name!r} already exists") from exc
        return await self.get(endpoint.uid)

    async def update(self, ref: str, data: EndpointUpdateRequest, if_match: str | None = None) -> EndpointSchema:
        """Replaces the declared half, and the target set as a whole when it is given.

        The replacement empties the collection and flushes before attaching the new
        rows: `(endpoint_id, deployment_id)` is unique, and a single flush orders
        every insert before the deletes, so re-weighting a target the endpoint
        already has would collide with the row it is about to replace.
        """
        row = await self._one(ref)
        endpoint = row[0]
        if if_match is not None and if_match != _revision(endpoint):
            raise Conflict("endpoint changed since it was read", {"revision": _revision(endpoint)})

        fields = {k: v for k, v in data.to_dict().items() if v is not UNSET}
        check_shape(
            fields.get("visibility", endpoint.visibility),
            fields.get("routing_strategy", endpoint.routing_strategy),
        )

        if "targets" in fields or "failover_order" in fields:
            targets = fields.pop("targets", UNSET)
            if targets is UNSET:
                raise Invalid("failover order can only be changed alongside the targets it orders")
            check_targets(targets)
            rows, order = await self._resolve(targets, fields.pop("failover_order", []))
            endpoint.targets = []
            await self.db.flush()
            endpoint.targets = rows
            endpoint.failover_order = order

        for key, value in fields.items():
            setattr(endpoint, key, value)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("endpoint conflicts with an existing one") from exc
        return await self.get(endpoint.uid)

    async def delete(self, ref: str) -> None:
        """Deletes the endpoint and its targets; the deployments behind it stay.

        A target is the association, not the thing associated. Removing a public
        name must never take the capacity serving it with it.
        """
        row = await self._one(ref)
        await self.repo.delete(row[0])
        await self.db.commit()

    async def resolved(self, ref: str | None = None) -> ResolvedTableSchema:
        """The routing table: every enabled endpoint, with its targets as addresses.

        Derived on every read and stored nowhere. The endpoint is what somebody
        declared; this is what that currently means, and materialising it would
        create a second copy of the same fact with its own way of being wrong.

        A target is dropped when it is disabled, when its deployment is disabled,
        or when the cluster it sits on has no serving address or has had one
        turned off — each of those is somebody saying "not this one", and the
        endpoint survives all of them with fewer upstreams.

        The serving quota is one row per endpoint rather than one per target, so
        its stamp goes into `own` beside the endpoint's and not into the row loop.
        Its limits are passed through undivided: how many gateway replicas are
        running is a fact of the process reading this, not of the plane writing
        it, so the fleet-wide numbers travel and `limit_scope` says how to read
        them.
        """
        rows = await self.repo.resolved(ref)
        if ref is not None and not rows:
            raise NotFound(f"no endpoint {ref!r}")

        order: list[UUID] = []
        grouped: dict[UUID, list] = {}
        endpoints: dict[UUID, Endpoint] = {}
        for row in rows:
            endpoint = row[0]
            if endpoint.id not in grouped:
                order.append(endpoint.id)
                grouped[endpoint.id] = []
                endpoints[endpoint.id] = endpoint
            grouped[endpoint.id].append(row)

        stamps: list[dt.datetime | None] = []
        items: list[ResolvedEndpointSchema] = []
        for endpoint_id in order:
            endpoint = endpoints[endpoint_id]
            quota = grouped[endpoint_id][0][5]
            own: list[dt.datetime | None] = [_stamp(endpoint), _stamp(quota)]
            upstreams: list[ResolvedUpstreamSchema] = []
            uids: dict[UUID, str] = {}

            for _, target, deployment, cluster, serving, _ in grouped[endpoint_id]:
                if target is None:
                    continue
                own.extend([_stamp(target), _stamp(deployment), _stamp(serving)])
                if deployment is not None:
                    uids[deployment.id] = deployment.uid
                if not target.enabled or deployment is None or not deployment.enabled:
                    continue
                if serving is None or not serving.enabled:
                    continue
                upstreams.append(
                    ResolvedUpstreamSchema(
                        deployment_id=deployment.uid,
                        deployment_name=deployment.name,
                        admission_phase=deployment.admission_phase,
                        cluster_id=cluster.uid,
                        cluster_name=cluster.name,
                        url=serving.url,
                        credential_id=str(serving.credential_id) if serving.credential_id else None,
                        weight=target.weight,
                        role=target.role,
                    )
                )

            stamps.extend(own)
            items.append(
                ResolvedEndpointSchema(
                    id=endpoint.uid,
                    name=endpoint.name,
                    visibility=endpoint.visibility,
                    routing_strategy=endpoint.routing_strategy,
                    upstreams=upstreams,
                    failover_order=[uids[d] for d in endpoint.failover_order if d in uids],
                    limits=declared_limits(quota) if quota is not None else None,
                    revision=_revision_of(own),
                )
            )

        return ResolvedTableSchema(revision=_revision_of(stamps), endpoints=items)
