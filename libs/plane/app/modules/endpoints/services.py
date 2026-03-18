from __future__ import annotations

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

from .schemas import EndpointSchema
from .schemas import EndpointTargetSchema
from .schemas import EndpointCreateRequest
from .schemas import EndpointTargetRequest
from .schemas import EndpointUpdateRequest
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
        await self.db.refresh(endpoint, ["targets"])
        return await self._render(endpoint, project.uid)

    async def update(self, ref: str, data: EndpointUpdateRequest, if_match: str | None = None) -> EndpointSchema:
        row = await self._one(ref)
        endpoint, project_uid = row[0], row.project_uid
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
            endpoint.targets = rows
            endpoint.failover_order = order

        for key, value in fields.items():
            setattr(endpoint, key, value)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("endpoint conflicts with an existing one") from exc
        await self.db.refresh(endpoint, ["targets"])
        return await self._render(endpoint, project_uid)

    async def delete(self, ref: str) -> None:
        """Deletes the endpoint and its targets; the deployments behind it stay.

        A target is the association, not the thing associated. Removing a public
        name must never take the capacity serving it with it.
        """
        row = await self._one(ref)
        await self.repo.delete(row[0])
        await self.db.commit()
