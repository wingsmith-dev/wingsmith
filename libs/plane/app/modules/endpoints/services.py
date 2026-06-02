from __future__ import annotations

import hashlib
import datetime as dt

from uuid import UUID

from msgspec import UNSET
from msgspec import ValidationError
from msgspec import convert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import ENDPOINT
from app.core.ids import new_uid
from app.db.models import Endpoint
from app.db.models import EndpointTarget
from app.core.schemas import ListResponse
from app.core.serving import served_model
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
from .schemas import ParameterPolicySchema
from .schemas import ResolvedEndpointSchema
from .schemas import ResolvedUpstreamSchema
from .repository import EndpointRepository


VISIBILITIES = frozenset({"private", "public"})
STRATEGIES = frozenset({"weighted", "failover", "latency"})
ROLES = frozenset({"primary", "canary", "fallback"})
SERVING_PHASES = frozenset({"active"})
"""The cluster phases that may carry inference traffic.

`pending` has never been enrolled, `draining` is being emptied on purpose and
`disabled` is out of service — sending requests to any of them is the plane
contradicting the intent somebody declared. Stated here rather than imported from
the clusters module for the same reason `PLACEABLE_PHASES` is stated in
`placement`: which phases serve is this reader's rule, not the vocabulary's."""


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


def public_model(endpoint: Endpoint) -> str:
    """The model id clients send for this endpoint.

    A read of one column now. It used to be a rule — the slug, else the name — with a
    `resolved_model_slug` computed on the way out so that an operator could see which of the two
    had won. That is what a nullable public identifier costs: a second field to report the answer,
    and two call sites that have to agree or the console documents a model id that does not route.

    Kept as a function because the routing key comes from here and not from `Endpoint.name`, and
    that is the whole reason a public id can be corrected without touching a cluster: the gateway
    is told what to route on every time it polls (`next[route.Name]` in `routing/table.go`).
    """
    return str(endpoint.slug)


def check_shape(visibility: str, routing_strategy: str) -> None:
    if visibility not in VISIBILITIES:
        raise Invalid(f"unknown visibility {visibility!r}", {"allowed": sorted(VISIBILITIES)})
    if routing_strategy not in STRATEGIES:
        raise Invalid(f"unknown routing strategy {routing_strategy!r}", {"allowed": sorted(STRATEGIES)})


def parameter_policy(surface: dict | None) -> ParameterPolicySchema | None:
    """The endpoint's request terms, or None when it has not sold any.

    Read on every table build and validated on every write, both through this one
    function, so a policy that would not survive the gateway cannot be stored.
    """
    raw = (surface or {}).get("policy")
    if raw is None:
        return None
    try:
        return convert(raw, ParameterPolicySchema)
    except ValidationError as exc:
        raise Invalid(f"surface.policy is not a parameter policy: {exc}", {"field": "surface.policy"}) from exc


SURFACE_CHOICES = {
    "protocols": frozenset({"openai.chat", "openai.completions", "openai.responses", "openai.embeddings"}),
    "auth_mode": frozenset({"none", "api_key", "mtls"}),
    "error_shape": frozenset({"passthrough"}),
    "streaming_transport": frozenset({"sse", "none"}),
    "output_length_params": frozenset({"max_tokens", "max_completion_tokens", "max_output_tokens"}),
}
"""The values each surface field may take, matching the console's own constants.

Checked here because the surface is patchable and half of it the gateway acts on.
An unchecked `streaming_transport` is a term an operator reads off a page and
repeats to a customer, and nothing downstream would have caught it.
"""

SURFACE_LISTS = frozenset({"protocols", "output_length_params"})
SURFACE_SCALARS = {"auth_mode", "error_shape", "streaming_transport"}


def check_surface(surface: dict) -> None:
    """Refuse a surface that is not one the gateway and console can read."""
    if not isinstance(surface, dict):
        raise Invalid("surface must be an object", {"field": "surface"})

    for field in SURFACE_LISTS:
        values = surface.get(field)
        if values is None:
            continue
        if not isinstance(values, list) or any(v not in SURFACE_CHOICES[field] for v in values):
            raise Invalid(
                f"surface.{field} has a value that is not allowed",
                {"field": f"surface.{field}", "allowed": sorted(SURFACE_CHOICES[field])},
            )

    for field in SURFACE_SCALARS:
        value = surface.get(field)
        if value is not None and value not in SURFACE_CHOICES[field]:
            raise Invalid(
                f"surface.{field} {value!r} is not allowed",
                {"field": f"surface.{field}", "allowed": sorted(SURFACE_CHOICES[field])},
            )

    if surface.get("cors_enabled") is not None and not isinstance(surface["cors_enabled"], bool):
        raise Invalid("surface.cors_enabled must be true or false", {"field": "surface.cors_enabled"})

    for field in ("max_request_bytes", "request_timeout_seconds"):
        value = surface.get(field)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise Invalid(f"surface.{field} must be a positive whole number", {"field": f"surface.{field}"})

    policy = parameter_policy(surface)
    if policy is None:
        return
    for name, rule in policy.parameters.items():
        if rule.enum is None and rule.min is None and rule.max is None:
            raise Invalid(
                f"surface.policy.parameters.{name} constrains nothing — give enum, min or max",
                {"field": f"surface.policy.parameters.{name}"},
            )
        if rule.min is not None and rule.max is not None and rule.min > rule.max:
            raise Invalid(
                f"surface.policy.parameters.{name} has min above max",
                {"field": f"surface.policy.parameters.{name}"},
            )


def _revision(endpoint: Endpoint) -> str:
    """The value `If-Match` is checked against, derived from the last write."""
    stamp = endpoint.updated_at or endpoint.created_at
    return str(int(stamp.timestamp() * 1_000_000))


def _stamp(row) -> dt.datetime | None:
    """When a row last moved, for the revision the whole table is versioned by."""
    if row is None:
        return None
    return row.updated_at or row.created_at


def _served(values: list[str]) -> str:
    """The third revision term: what the engines behind this table answer to.

    A short hash over the serving digests and resolved model names a resolution
    was built from. It is content rather than a timestamp, and that is the whole
    reason it exists: it moves when, and only when, what the gateway has to
    rewrite an inbound `model` to has actually changed.

    It replaces a stamp taken off the recipe row, which was a proxy for the same
    question and wrong in both directions. Renaming a recipe, or editing a field
    that never reached an engine, pushed the revision forward and made every
    gateway re-fetch a table that had not changed. Meanwhile the workload's own
    `served_model_name` was never stamped at all, so declaring one moved nothing
    and a gateway sat behind a `304` rewriting to the name it had before.

    Sorted and deduplicated so the value depends on the set and not on the row
    order the database happened to return. Truncated because this is a change
    detector, not a signature: a collision costs one stale `304` against a table
    that also has two other terms moving under it.
    """
    if not values:
        return "0"
    body = "\x1f".join(sorted(set(values)))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _revision_of(stamps: list[dt.datetime | None], served: list[str]) -> str:
    """The newest stamp a resolution was built from, how many there were, and what they serve.

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
    because the added row is freshly stamped.

    The third term is `_served`, and it is content rather than time. The value
    stays opaque to its readers, which compare it as a string.
    """
    live = [stamp for stamp in stamps if stamp is not None]
    if not live:
        return f"0-0-{_served(served)}"
    return f"{int(max(live).timestamp() * 1_000_000)}-{len(live)}-{_served(served)}"


class EndpointService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = EndpointRepository(db)

    def schema(self, endpoint: Endpoint, project_uid: str, uids: dict[UUID, str]) -> EndpointSchema:
        return EndpointSchema(
            id=endpoint.uid,
            name=endpoint.name,
            slug=endpoint.slug,
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
                    served_model_name=t.served_model_name,
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
        workload: str | None = None,
        search: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> ListResponse[EndpointSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            project=project,
            artifact=artifact,
            workload=workload,
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
                served_model_name=(t.served_model_name or "").strip() or None,
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

        # Defaulted from the weights behind the first target, then from the endpoint's own name.
        #
        # The default is the point of the field rather than a convenience. An endpoint in front of
        # one model answers to that model's name, and requiring a caller to type it is how the
        # public id and the id the engine registered come to differ — which is not a visible
        # failure but a 404 from inside the cluster, with the deployment healthy and the tunnel
        # open. Declaring one is then unambiguously a request for an alias.
        slug = (data.slug or "").strip().lower()
        if not slug and targets:
            slug = await self.repo.artifact_slug(targets[0].deployment_id)
        if not slug:
            slug = str(data.name)

        endpoint = Endpoint(
            uid=new_uid(ENDPOINT),
            project_id=project.id,
            name=data.name,
            slug=slug,
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
        if "surface" in fields:
            check_surface(fields["surface"])
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

        if "slug" in fields:
            # Lowercased and never emptied. This is the public model id, so a caller who clears it
            # has said nothing about what clients should send, and the honest reading of that is
            # "leave it" rather than "serve under the empty string".
            slug = str(fields.pop("slug")).strip().lower()
            if slug:
                endpoint.slug = slug

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
        when the cluster it sits on is not in a serving phase, or when that
        cluster has declared a serving address and then turned it off — each of
        those is somebody saying "not this one", and the endpoint survives all of
        them with fewer upstreams.

        A cluster with no `ClusterServing` row at all is *not* one of them, and it
        is the normal case: inference reaches a cluster over the tunnel its
        serving agent dialled out, so there is no address to publish and
        nothing declares one. Such a target resolves with an empty `url`, and the
        `cluster_id` beside it is the whole of the routing key — the gateway keys
        its tunnel pool by exactly that and dials the published address only when
        there is one. Dropping these rows would make the table empty on every
        cluster we run, which reads as "no model is serving" rather than as
        "nobody publishes an ingress any more".

        Every one of those is decided here rather than in the query, because the
        query is an outer join the whole way down and a `WHERE` on the cluster
        would drop the endpoint's last row along with its last upstream — turning
        "this model has nowhere to go" into "no such model". The cluster's stamp
        goes into the revision for the same reason the others do: marking a
        cluster `draining` changes this table, and a gateway sitting behind a
        `304` would keep routing to it.

        Each upstream carries the name the engine behind it answers to, because
        the public name and the served name are not the same string and nothing
        makes them agree: a workload may declare a `served_model_name` of its
        own, and otherwise the name resolves to the artifact's Hugging Face slug.
        A client calling the endpoint by its public name would be asking the
        frontend for a model it does not have, so the gateway rewrites it.

        That resolution goes into the revision as content — the workload's
        `serving_digest` and the resolved name itself — rather than as a stamp
        taken off the row it was read from. It used to be the recipe's
        `updated_at`, and a stamp is a proxy for the question that is wrong in
        both directions: it moved when a recipe was renamed or when a field that
        never reaches an engine was edited, making every gateway re-fetch an
        unchanged table, and it said nothing at all about the workload's own
        `served_model_name`, which was never stamped — so declaring one left the
        gateway behind a `304`, rewriting to the name it had before. A digest
        moves on a real configuration change and on nothing else.

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
        resolved_names: list[str] = []
        items: list[ResolvedEndpointSchema] = []
        for endpoint_id in order:
            endpoint = endpoints[endpoint_id]
            quota = grouped[endpoint_id][0][5]
            own: list[dt.datetime | None] = [_stamp(endpoint), _stamp(quota)]
            upstreams: list[ResolvedUpstreamSchema] = []
            uids: dict[UUID, str] = {}

            served: list[str] = []
            for _, target, deployment, cluster, serving, _, artifact, workload in grouped[endpoint_id]:
                if target is None:
                    continue
                own.extend([_stamp(target), _stamp(deployment), _stamp(cluster), _stamp(serving)])
                own.append(_stamp(artifact))
                if workload is not None:
                    served.append(workload.serving_digest)
                if deployment is not None:
                    uids[deployment.id] = deployment.uid
                if not target.enabled or deployment is None or not deployment.enabled:
                    continue
                if cluster is None or cluster.phase not in SERVING_PHASES:
                    continue
                if serving is not None and not serving.enabled:
                    continue
                name = (target.served_model_name or "").strip() or (
                    served_model(workload, artifact) if workload is not None and artifact is not None else ""
                )
                served.append(name)
                upstreams.append(
                    ResolvedUpstreamSchema(
                        deployment_id=deployment.uid,
                        deployment_name=deployment.name,
                        admission_phase=deployment.admission_phase,
                        cluster_id=cluster.uid,
                        cluster_name=cluster.name,
                        url=serving.url if serving is not None else "",
                        served_model_name=name,
                        credential_id=str(serving.credential_id)
                        if serving is not None and serving.credential_id
                        else None,
                        weight=target.weight,
                        role=target.role,
                    )
                )

            stamps.extend(own)
            resolved_names.extend(served)
            items.append(
                ResolvedEndpointSchema(
                    id=endpoint.uid,
                    name=public_model(endpoint),
                    visibility=endpoint.visibility,
                    routing_strategy=endpoint.routing_strategy,
                    upstreams=upstreams,
                    failover_order=[uids[d] for d in endpoint.failover_order if d in uids],
                    limits=declared_limits(quota) if quota is not None else None,
                    policy=parameter_policy(endpoint.surface),
                    revision=_revision_of(own, served),
                )
            )

        return ResolvedTableSchema(revision=_revision_of(stamps, resolved_names), endpoints=items)
