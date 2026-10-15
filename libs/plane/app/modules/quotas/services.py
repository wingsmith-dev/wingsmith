import datetime as dt

from sqlalchemy import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import QUOTA
from app.core.ids import SERVING_QUOTA
from app.core.ids import new_uid
from app.db.models import Quota
from app.db.models import ServingQuota
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor
from app.modules.metrics.objectives import AVAILABILITY_OBJECTIVES
from app.modules.metrics.objectives import DEFAULT_AVAILABILITY_OBJECTIVE

from .schemas import QuotaSchema
from .schemas import QuotaUsageSchema
from .schemas import QuotaCreateRequest
from .schemas import QuotaUpdateRequest
from .schemas import ServingQuotaSchema
from .schemas import ResolvedLimitsSchema
from .schemas import ServingQuotaCreateRequest
from .schemas import ServingQuotaUpdateRequest
from .schemas import ResolvedServingQuotaSchema
from .schemas import ServingQuotaStandardSchema
from .schemas import ResolvedServingQuotaTableSchema
from .repository import QuotaRepository
from .repository import ServingQuotaRepository


EMPTY = QuotaUsageSchema(allocated_devices=0, live_allocations=0, over_quota_devices=0)


def _revision(quota: Quota) -> str:
    """The value `If-Match` is checked against, derived from the last write."""
    stamp = quota.updated_at or quota.created_at
    return str(int(stamp.timestamp() * 1_000_000))


def usage_for(quota: Quota, rollup: dict) -> QuotaUsageSchema:
    """Folds the per-(project, cluster, class) rollup down to one quota's scope.

    A quota with no cluster spans every cluster, so its usage is the sum of all
    the cluster-keyed rows for its project and device class rather than the row
    whose cluster is null — no allocation is ever recorded without a cluster, so
    reading a null-cluster quota as a null-cluster lookup would always report
    zero usage against a limit that is really being consumed.
    """
    devices = allocations = over_quota = 0
    for (project_id, cluster_id, device_class), row in rollup.items():
        if project_id != quota.project_id or device_class != quota.device_class:
            continue
        if quota.cluster_id is not None and cluster_id != quota.cluster_id:
            continue
        devices += row["devices"]
        allocations += row["allocations"]
        over_quota += row["over_quota_devices"]
    return QuotaUsageSchema(
        allocated_devices=devices,
        live_allocations=allocations,
        over_quota_devices=over_quota,
    )


def headroom(limit_devices: int | None, allocated: int) -> int | None:
    """Devices still available under the hard limit. Null when there is no limit."""
    if limit_devices is None:
        return None
    return limit_devices - allocated


def over_limit(limit_devices: int | None, allocated: int) -> bool:
    if limit_devices is None:
        return False
    return allocated > limit_devices


def validate_numbers(guaranteed: int, limit: int | None, weight: float) -> None:
    """Rejects a quota that cannot mean anything.

    A limit below the guarantee is the interesting case: the two numbers are a
    floor and a ceiling, and a ceiling under the floor promises capacity in one
    field and forbids it in the other.
    """
    if guaranteed < 0:
        raise Invalid("guaranteed_devices cannot be negative", {"guaranteed_devices": guaranteed})
    if limit is not None and limit < 0:
        raise Invalid("limit_devices cannot be negative", {"limit_devices": limit})
    if limit is not None and limit < guaranteed:
        raise Invalid(
            "limit_devices is below guaranteed_devices",
            {"guaranteed_devices": guaranteed, "limit_devices": limit},
        )
    if weight < 0:
        raise Invalid("over_quota_weight cannot be negative", {"over_quota_weight": weight})


class QuotaService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = QuotaRepository(db)

    def schema(self, row: Row, usage: QuotaUsageSchema) -> QuotaSchema:
        quota: Quota = row[0]
        return QuotaSchema(
            id=quota.uid,
            project=row.project_uid,
            project_slug=row.project_slug,
            cluster=row.cluster_uid,
            cluster_name=row.cluster_name,
            device_class=quota.device_class,
            guaranteed_devices=quota.guaranteed_devices,
            limit_devices=quota.limit_devices,
            over_quota_weight=float(quota.over_quota_weight),
            over_quota_enabled=quota.over_quota_enabled,
            created_at=quota.created_at,
            updated_at=quota.updated_at,
            revision=_revision(quota),
            usage=usage,
            headroom_devices=headroom(quota.limit_devices, usage.allocated_devices),
            over_limit=over_limit(quota.limit_devices, usage.allocated_devices),
        )

    async def _one(self, ref: str, lock: bool = False) -> Row:
        row = await self.repo.get(ref, lock=lock)
        if row is None:
            raise NotFound(f"no quota {ref!r}")
        return row

    async def _with_usage(self, rows: list[Row]) -> list[QuotaSchema]:
        rollup = await self.repo.usage([r[0].project_id for r in rows])
        return [self.schema(r, usage_for(r[0], rollup)) for r in rows]

    async def get(self, ref: str) -> QuotaSchema:
        row = await self._one(ref)
        return (await self._with_usage([row]))[0]

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        project: str | None = None,
        cluster: str | None = None,
        device_class: str | None = None,
        over_quota_enabled: bool | None = None,
    ) -> ListResponse[QuotaSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            project=project,
            cluster=cluster,
            device_class=device_class,
            over_quota_enabled=over_quota_enabled,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = await self._with_usage(kept)
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def create(self, data: QuotaCreateRequest) -> QuotaSchema:
        validate_numbers(data.guaranteed_devices, data.limit_devices, data.over_quota_weight)
        if not data.device_class:
            raise Invalid("device_class is required")
        project = await self.repo.project_by_ref(data.project)
        if project is None:
            raise NotFound(f"no project {data.project!r}")
        cluster = None
        if data.cluster is not None:
            cluster = await self.repo.cluster_by_ref(data.cluster)
            if cluster is None:
                raise NotFound(f"no cluster {data.cluster!r}")
        quota = Quota(
            uid=new_uid(QUOTA),
            project_id=project.id,
            cluster_id=cluster.id if cluster else None,
            device_class=data.device_class,
            guaranteed_devices=data.guaranteed_devices,
            limit_devices=data.limit_devices,
            over_quota_weight=data.over_quota_weight,
            over_quota_enabled=data.over_quota_enabled,
        )
        try:
            await self.repo.add(quota)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(
                f"a quota for {data.device_class!r} in {data.project!r} already exists at that scope"
            ) from exc
        return await self.get(quota.uid)

    async def update(self, ref: str, data: QuotaUpdateRequest, if_match: str | None = None) -> QuotaSchema:
        row = await self._one(ref, lock=True)
        quota: Quota = row[0]
        if if_match is not None and if_match != _revision(quota):
            raise Conflict("quota changed since it was read", {"revision": _revision(quota)})

        fields = data.to_dict(exclude_unset=True)
        validate_numbers(
            fields.get("guaranteed_devices", quota.guaranteed_devices),
            fields.get("limit_devices", quota.limit_devices),
            fields.get("over_quota_weight", float(quota.over_quota_weight)),
        )
        for key, value in fields.items():
            setattr(quota, key, value)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("quota conflicts with an existing one") from exc
        return await self.get(quota.uid)

    async def delete(self, ref: str) -> None:
        row = await self._one(ref)
        await self.repo.delete(row[0])
        await self.db.commit()


LIMIT_SCOPES = frozenset({"per_replica", "divided"})
"""How a declared number would be read by a fleet of gateway replicas once one reads it.

`per_replica` gives every replica the whole number, so R replicas would admit up
to R times what was sold. `divided` gives each replica its share, so an
uneven connection balance would refuse a caller who is inside the limit it bought.
The design has no shared counter and no coordination hop on the request path, which
keeps the gateway's availability a function of its own process, so one of those two
error directions is chosen rather than avoided.
"""

ON_EXCEED = frozenset({"reject", "queue"})

DEFAULT_OUTPUT_LENGTH_PARAMS = ("max_completion_tokens", "max_tokens", "max_output_tokens")

STANDARD_RPM = 1_000
STANDARD_TPM = 10_000_000
STANDARD_SUCCESS_RATE = 0.999
"""The floors the acceptance standard asks a served model to commit to.

Compared against what was declared, never against what was measured. A quota
that clears them is a promise that meets the standard; whether it is kept is the
gateway's measurement and the metrics surface's to report.
"""

MIN_OBJECTIVE_WINDOW = 60
MAX_OBJECTIVE_WINDOW = 3_600

POSITIVE_LIMITS = (
    "committed_rpm",
    "committed_tpm",
    "max_concurrent_requests",
    "max_output_tokens",
    "burst_rpm",
)

DIVISIBLE_LIMITS = ("committed_rpm", "committed_tpm", "max_concurrent_requests", "burst_rpm")
"""The limits that are a fleet budget and therefore divide.

`max_output_tokens` is deliberately not among them: it is a ceiling on what one
request may ask for, not an allowance spent over a minute, and a fleet of four
that let each replica accept a quarter of the sold context would be refusing
every caller. `ramp_up_seconds` and `queue_timeout_ms` are durations for the same
reason.
"""


def replica_share(limit: int | None, replicas: int, index: int) -> int | None:
    """One replica's slice of a fleet-wide limit, under `divided`.

    Largest remainder rather than `limit // replicas`: the floor alone throws away
    `limit % replicas` of what was sold on every uneven split — 1,000 rpm across
    three replicas would enforce 999 — and the ceiling over-admits by as much. The
    remainder goes to the lowest-numbered replicas, so the shares sum to exactly
    the declared limit at any replica count, and one index always gets the same
    slice, which is what keeps a restart from handing a pod a different share.

    An uncommitted limit stays uncommitted. Dividing null would turn "not limited
    on this axis" into a number, and at any fleet larger than the limit that
    number would be zero.
    """
    if limit is None:
        return None
    base, remainder = divmod(limit, replicas)
    return base + (1 if index < remainder else 0)


def check_fleet(replicas: int, index: int) -> None:
    """Rejects a fleet a share cannot be taken of.

    The plane never discovers the replica count on its own — doing so would put a
    Kubernetes client on the inference path — so it arrives from the caller and is
    checked here rather than trusted.
    """
    if replicas < 1:
        raise Invalid("replicas must be at least 1", {"replicas": replicas})
    if not 0 <= index < replicas:
        raise Invalid(
            "replica_index is outside the fleet",
            {"replicas": replicas, "replica_index": index},
        )


def starved_replicas(quota: ServingQuota, replicas: int) -> int:
    """How many replicas would enforce a hard zero on some limit under `divided`.

    A commitment smaller than the fleet cannot be shared out: 2 rpm across five
    replicas gives two of them one each and the other three nothing, and a replica
    holding a share of zero refuses every request that reaches it. The starved
    replicas are always the highest-numbered ones, because the remainder goes to
    the lowest, so the union across limits is a count and not a set.

    Reported rather than refused: which replica a caller lands on is the load
    balancer's business, and an operator lowering a fleet below a sold rate needs
    to see the consequence rather than be told the number is invalid.
    """
    if quota.limit_scope != "divided":
        return 0
    short = [
        replicas - limit
        for limit in (getattr(quota, name) for name in DIVISIBLE_LIMITS)
        if limit is not None and limit < replicas
    ]
    return max(short, default=0)


def declared_limits(quota: ServingQuota) -> ResolvedLimitsSchema:
    """What was sold, across the whole fleet."""
    return ResolvedLimitsSchema(
        committed_rpm=quota.committed_rpm,
        burst_rpm=quota.burst_rpm,
        ramp_up_seconds=quota.ramp_up_seconds,
        committed_tpm=quota.committed_tpm,
        max_concurrent_requests=quota.max_concurrent_requests,
        max_output_tokens=quota.max_output_tokens,
        on_exceed=quota.on_exceed,
        queue_timeout_ms=quota.queue_timeout_ms,
        limit_scope=quota.limit_scope,
        enforced=quota.enforced,
    )


def effective_limits(quota: ServingQuota, replicas: int = 1, index: int = 0) -> ResolvedLimitsSchema:
    """The share of the declared limits the replica at `index` would hold, once `limit_scope` is applied.

    Under `per_replica` this is the declared set unchanged — that is what the mode
    means — and the arithmetic below is skipped rather than run with a divisor of
    one, so the two modes cannot converge by accident at a single replica.
    """
    if quota.limit_scope != "divided":
        return declared_limits(quota)
    return ResolvedLimitsSchema(
        committed_rpm=replica_share(quota.committed_rpm, replicas, index),
        burst_rpm=replica_share(quota.burst_rpm, replicas, index),
        ramp_up_seconds=quota.ramp_up_seconds,
        committed_tpm=replica_share(quota.committed_tpm, replicas, index),
        max_concurrent_requests=replica_share(quota.max_concurrent_requests, replicas, index),
        max_output_tokens=quota.max_output_tokens,
        on_exceed=quota.on_exceed,
        queue_timeout_ms=quota.queue_timeout_ms,
        limit_scope=quota.limit_scope,
        enforced=quota.enforced,
    )


def standard_of(
    committed_rpm: int | None,
    committed_tpm: int | None,
    success_rate_objective: float | None,
) -> ServingQuotaStandardSchema:
    """Compares a declared commitment against the standard's three floors."""
    shortfalls: list[str] = []
    if committed_rpm is None or committed_rpm < STANDARD_RPM:
        shortfalls.append("committed_rpm")
    if committed_tpm is None or committed_tpm < STANDARD_TPM:
        shortfalls.append("committed_tpm")
    if success_rate_objective is None or float(success_rate_objective) < STANDARD_SUCCESS_RATE:
        shortfalls.append("success_rate_objective")
    return ServingQuotaStandardSchema(meets=not shortfalls, shortfalls=shortfalls)


def output_length_params(surface: dict | None) -> list[str]:
    """Which request fields carry the caller's requested output length.

    Declared on the endpoint's surface, because which fields an API accepts is a
    property of the API rather than of the commitment. Absent means the default
    set and not none: an endpoint that has never said otherwise speaks the
    ordinary chat surface, and reading absence as "no field" would refuse a
    ceiling on every endpoint that exists today.
    """
    declared = (surface or {}).get("output_length_params")
    if declared is None:
        return list(DEFAULT_OUTPUT_LENGTH_PARAMS)
    return list(declared)


def validate_serving(fields: dict, surface: dict | None = None) -> None:
    """Rejects a commitment that cannot mean anything.

    Takes the row as it would be after the write, never the patch, so that a
    partial update is checked against what it produces rather than against what
    it mentions — a `burst_rpm` lowered under an untouched `committed_rpm` is the
    case that a patch-only check misses.
    """
    for name in POSITIVE_LIMITS:
        value = fields.get(name)
        if value is not None and value <= 0:
            raise Invalid(f"{name} must be positive when it is set", {name: value})

    committed_rpm = fields.get("committed_rpm")
    burst_rpm = fields.get("burst_rpm")
    if committed_rpm is not None and burst_rpm is not None and burst_rpm < committed_rpm:
        raise Invalid(
            "burst_rpm is below committed_rpm",
            {"committed_rpm": committed_rpm, "burst_rpm": burst_rpm},
        )

    objective = fields.get("success_rate_objective")
    if objective is not None and not 0 < float(objective) <= 1:
        raise Invalid(
            "success_rate_objective is a fraction in (0, 1], not a percentage",
            {"success_rate_objective": float(objective)},
        )

    on_exceed = fields.get("on_exceed", "reject")
    if on_exceed not in ON_EXCEED:
        raise Invalid(f"unknown on_exceed {on_exceed!r}", {"allowed": sorted(ON_EXCEED)})

    queue_timeout_ms = fields.get("queue_timeout_ms")
    if on_exceed == "queue" and queue_timeout_ms is None:
        raise Invalid("on_exceed 'queue' needs a queue_timeout_ms to bound the wait")
    if on_exceed != "queue" and queue_timeout_ms is not None:
        raise Invalid(
            "queue_timeout_ms means nothing unless on_exceed is 'queue'",
            {"on_exceed": on_exceed},
        )
    if queue_timeout_ms is not None and queue_timeout_ms <= 0:
        raise Invalid("queue_timeout_ms must be positive", {"queue_timeout_ms": queue_timeout_ms})

    limit_scope = fields.get("limit_scope", "per_replica")
    if limit_scope not in LIMIT_SCOPES:
        raise Invalid(f"unknown limit_scope {limit_scope!r}", {"allowed": sorted(LIMIT_SCOPES)})

    ramp_up_seconds = fields.get("ramp_up_seconds", 0)
    if ramp_up_seconds < 0:
        raise Invalid("ramp_up_seconds cannot be negative", {"ramp_up_seconds": ramp_up_seconds})

    availability = fields.get("availability_objective", DEFAULT_AVAILABILITY_OBJECTIVE)
    if availability not in AVAILABILITY_OBJECTIVES:
        raise Invalid(
            f"unknown availability objective {availability!r}",
            {"allowed": sorted(AVAILABILITY_OBJECTIVES)},
        )

    window = fields.get("objective_window_seconds", 600)
    if not MIN_OBJECTIVE_WINDOW <= window <= MAX_OBJECTIVE_WINDOW:
        raise Invalid(
            "objective_window_seconds is outside 60..3600",
            {"objective_window_seconds": window},
        )

    if fields.get("max_output_tokens") is not None and not output_length_params(surface):
        raise Invalid(
            "max_output_tokens needs the endpoint's surface to name where a requested length is read from",
            {"endpoint_field": "surface.output_length_params"},
        )


def _serving_revision(quota: ServingQuota) -> str:
    """The value `If-Match` is checked against, derived from the last write.

    Unrelated to the resolved table's revision, which versions a different thing
    on a different surface.
    """
    stamp = quota.updated_at or quota.created_at
    return str(int(stamp.timestamp() * 1_000_000))


def _stamp(row) -> dt.datetime | None:
    if row is None:
        return None
    return row.updated_at or row.created_at


def _table_revision(stamps: list[dt.datetime | None]) -> str:
    """The newest stamp a resolution was built from, and how many there were.

    Two terms, because a maximum alone cannot see a deletion: removing a row
    removes a stamp, and unless that stamp happened to be the maximum across the
    whole table the value does not move. A withdrawn commitment would then sit
    behind a `304` and go on being applied by every replica that reads it — the asymmetry is
    what makes it dangerous, since create and update move the maximum and only
    delete does not. The count moves on any disappearance, and the maximum still
    catches a simultaneous add and delete because the added row is freshly
    stamped.
    """
    live = [stamp for stamp in stamps if stamp is not None]
    if not live:
        return "0-0"
    return f"{int(max(live).timestamp() * 1_000_000)}-{len(live)}"


def fleet_revision(stamps: list[dt.datetime | None], replicas: int, index: int) -> str:
    """The resolved table's revision, for one fleet.

    The fleet is part of the answer, so it is part of the version: a gateway
    restarted with a different `replicas` must not be handed a `304` against the
    shares it computed for the old one.
    """
    return f"{_table_revision(stamps)}-{replicas}-{index}"


class ServingQuotaService:
    """Declares traffic commitments. Enforces nothing and measures nothing.

    Every method here reads or writes `bind_serving_quota` and rows this database
    already owns. Nothing counts a request, and nothing calls the metrics backend.
    Nothing enforces these numbers yet either: the gateway does not read serving
    quotas, so a declared limit is advisory until it does.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ServingQuotaRepository(db)

    def schema(self, row: Row) -> ServingQuotaSchema:
        quota: ServingQuota = row[0]
        objective = float(quota.success_rate_objective) if quota.success_rate_objective is not None else None
        return ServingQuotaSchema(
            id=quota.uid,
            endpoint=row.endpoint_uid,
            endpoint_name=row.endpoint_name,
            project=row.project_uid,
            committed_rpm=quota.committed_rpm,
            committed_tpm=quota.committed_tpm,
            max_concurrent_requests=quota.max_concurrent_requests,
            max_output_tokens=quota.max_output_tokens,
            burst_rpm=quota.burst_rpm,
            ramp_up_seconds=quota.ramp_up_seconds,
            on_exceed=quota.on_exceed,
            queue_timeout_ms=quota.queue_timeout_ms,
            limit_scope=quota.limit_scope,
            success_rate_objective=objective,
            availability_objective=quota.availability_objective,
            objective_window_seconds=quota.objective_window_seconds,
            enforced=quota.enforced,
            created_at=quota.created_at,
            updated_at=quota.updated_at,
            revision=_serving_revision(quota),
            standard=standard_of(quota.committed_rpm, quota.committed_tpm, objective),
        )

    async def _one(self, ref: str, lock: bool = False) -> Row:
        row = await self.repo.get(ref, lock=lock)
        if row is None:
            raise NotFound(f"no serving quota {ref!r}")
        return row

    async def get(self, ref: str) -> ServingQuotaSchema:
        return self.schema(await self._one(ref))

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        endpoint: str | None = None,
        project: str | None = None,
        enforced: bool | None = None,
        limit_scope: str | None = None,
    ) -> ListResponse[ServingQuotaSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            endpoint=endpoint,
            project=project,
            enforced=enforced,
            limit_scope=limit_scope,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = [self.schema(row) for row in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def create(self, data: ServingQuotaCreateRequest) -> ServingQuotaSchema:
        endpoint = await self.repo.endpoint_by_ref(data.endpoint)
        if endpoint is None:
            raise NotFound(f"no endpoint {data.endpoint!r}")

        fields = data.to_dict()
        fields.pop("endpoint")
        validate_serving(fields, endpoint.surface)

        quota = ServingQuota(uid=new_uid(SERVING_QUOTA), endpoint_id=endpoint.id, **fields)
        try:
            await self.repo.add(quota)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"endpoint {data.endpoint!r} already has a serving quota") from exc
        return await self.get(quota.uid)

    async def update(
        self,
        ref: str,
        data: ServingQuotaUpdateRequest,
        if_match: str | None = None,
    ) -> ServingQuotaSchema:
        row = await self._one(ref, lock=True)
        quota: ServingQuota = row[0]
        if if_match is not None and if_match != _serving_revision(quota):
            raise Conflict(
                "serving quota changed since it was read",
                {"revision": _serving_revision(quota)},
            )

        fields = data.to_dict(exclude_unset=True)
        merged = {name: getattr(quota, name) for name in ServingQuotaUpdateRequest.__struct_fields__}
        merged.update(fields)
        endpoint = await self.repo.endpoint_by_ref(row.endpoint_uid)
        validate_serving(merged, endpoint.surface if endpoint else None)

        for key, value in fields.items():
            setattr(quota, key, value)

        await self.db.commit()
        return await self.get(quota.uid)

    async def delete(self, ref: str) -> None:
        """Withdraws a commitment. The endpoint becomes unlimited, not zero-limited."""
        row = await self._one(ref)
        await self.repo.delete(row[0])
        await self.db.commit()

    async def resolved(
        self,
        endpoint: str | None = None,
        workload: str | None = None,
        replicas: int = 1,
        replica_index: int = 0,
    ) -> ResolvedServingQuotaTableSchema:
        """Every commitment that applies, resolved for a fleet of a stated size.

        Derived on every read and stored nowhere, the same way the routing table
        is: the quota is what somebody declared, and this is what it currently
        means for a fleet the plane does not otherwise know the size of.

        A reference that matches nothing comes back as an empty table rather than
        a `404`. An endpoint with no commitment is unlimited, and answering "no
        such thing" would let a caller read an unlimited endpoint as a missing
        one — which is the direction that turns a withdrawn quota into an outage.
        """
        check_fleet(replicas, replica_index)
        rows = await self.repo.resolved(endpoint=endpoint, workload=workload)

        order: list = []
        grouped: dict = {}
        for row in rows:
            quota = row[0]
            if quota.id not in grouped:
                order.append(quota.id)
                grouped[quota.id] = []
            grouped[quota.id].append(row)

        stamps: list[dt.datetime | None] = []
        items: list[ResolvedServingQuotaSchema] = []
        for quota_id in order:
            group = grouped[quota_id]
            quota, endpoint_row, project_uid = group[0][0], group[0][1], group[0].project_uid
            own: list[dt.datetime | None] = [_stamp(quota), _stamp(endpoint_row)]
            workloads: list[str] = []
            for _, _, _, target, workload_row in group:
                own.append(_stamp(target))
                if workload_row is not None and workload_row.uid not in workloads:
                    own.append(_stamp(workload_row))
                    workloads.append(workload_row.uid)

            objective = float(quota.success_rate_objective) if quota.success_rate_objective is not None else None
            stamps.extend(own)
            items.append(
                ResolvedServingQuotaSchema(
                    endpoint=endpoint_row.uid,
                    endpoint_name=endpoint_row.name,
                    project=project_uid,
                    workloads=workloads,
                    replicas=replicas,
                    replica_index=replica_index,
                    declared=declared_limits(quota),
                    effective=effective_limits(quota, replicas, replica_index),
                    starved_replicas=starved_replicas(quota, replicas),
                    success_rate_objective=objective,
                    availability_objective=quota.availability_objective,
                    objective_window_seconds=quota.objective_window_seconds,
                    standard=standard_of(quota.committed_rpm, quota.committed_tpm, objective),
                    revision=fleet_revision(own, replicas, replica_index),
                )
            )

        return ResolvedServingQuotaTableSchema(
            revision=fleet_revision(stamps, replicas, replica_index),
            replicas=replicas,
            replica_index=replica_index,
            quotas=items,
        )
