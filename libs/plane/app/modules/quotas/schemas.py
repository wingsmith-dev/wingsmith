import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType
from app.modules.metrics.objectives import DEFAULT_AVAILABILITY_OBJECTIVE


class QuotaUsageSchema(Struct):
    """The observed half of a quota: what the project is actually holding.

    Counted over live allocations at read time. It sits in its own object so that
    no field on the response is ever half declared and half observed: a single
    `used_devices` next to `guaranteed_devices` would read as one number someone
    set and one number that happened, spelled identically.
    """

    allocated_devices: int
    live_allocations: int
    over_quota_devices: int


class QuotaSchema(Struct):
    """A device ceiling per project and device class, with usage beside it, never merged.

    Placement enforces the ceiling only: `limit_devices`, or the lower of it and
    `guaranteed_devices` when `over_quota_enabled` is false. `guaranteed_devices`
    is not reserved for the project against other projects, and `over_quota_weight`
    is recorded but not read: there is no fair-share division of spare capacity.

    `cluster` is null when the quota spans every cluster, and usage is then
    summed across all of them. `over_limit` and `headroom_devices` are derived
    here, not in each client, because the console and `hgr` asking the
    same question must not get two answers.
    """

    id: str
    project: str
    project_slug: str
    cluster: str | None
    cluster_name: str | None
    device_class: str
    guaranteed_devices: int
    limit_devices: int | None
    over_quota_weight: float
    over_quota_enabled: bool
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str
    usage: QuotaUsageSchema
    headroom_devices: int | None
    over_limit: bool


class QuotaCreateRequest(Struct):
    project: str
    device_class: str
    cluster: str | None = None
    guaranteed_devices: int = 0
    limit_devices: int | None = None
    over_quota_weight: float = 1.0
    over_quota_enabled: bool = True


class QuotaUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `project`, `cluster` and `device_class` are absent by design: the three of
    them are the quota's unique scope, and changing one is creating a different
    quota instead of editing this one.
    """

    guaranteed_devices: int | UnsetType = UNSET
    limit_devices: int | None | UnsetType = UNSET
    over_quota_weight: float | UnsetType = UNSET
    over_quota_enabled: bool | UnsetType = UNSET


class ResolvedLimitsSchema(Struct):
    """The traffic ceiling for one endpoint, flattened for a hop that holds it in memory.

    Declared only, and not read by the gateway yet, so nothing enforces it today.
    There is no observed counterpart on this payload because its
    reader is the thing doing the observing, and handing the gateway back its own
    measurements would be a round trip whose only possible effect is to be stale.

    Nothing about the availability objective is here either. The objective is how
    the plane and the console interpret what the gateway measured; labelling an
    outcome needs none of that arithmetic, and shipping it would let a gateway
    version drift the definition of a commercial commitment.

    `limit_scope` is the one field that is not a limit but an instruction for how
    to read the limits beside it, and it has to travel with them: `per_replica`
    means the numbers are this replica's, `divided` means they are the fleet's and
    this replica's share is a fraction of them.
    """

    committed_rpm: int | None
    burst_rpm: int | None
    ramp_up_seconds: int
    committed_tpm: int | None
    max_concurrent_requests: int | None
    max_output_tokens: int | None
    on_exceed: str
    queue_timeout_ms: int | None
    limit_scope: str
    enforced: bool


class ServingQuotaStandardSchema(Struct):
    """Whether a declared commitment clears the acceptance standard's floors.

    Declared against declared, and no part of it is a measurement: it compares
    the numbers somebody promised against the numbers the standard asks to be
    promised, which is a question this database can answer on its own. Whether
    the endpoint then *delivers* them is the gateway's to measure and the metrics
    surface's to report, and the two must never be spelled the same.

    `shortfalls` names the fields, so a caller can point at the one that is short
    instead of re-deriving the comparison. An uncommitted field is a shortfall:
    the standard asks for a floor, and declaring nothing is not clearing it.
    """

    meets: bool
    shortfalls: list[str]


class ServingQuotaSchema(Struct):
    """One endpoint's committed request rate, token rate and concurrency.

    Advisory today. The gateway does not read serving quotas, so nothing refuses a
    request over these numbers; `enforced` records whether the operator wants them
    enforced once something does.

    Every field here is declared. There is no `usage`, and there is
    intentionally no observed sibling of the shape `QuotaSchema.usage` has: a device quota's
    usage is a join this database owns, while a serving quota's usage is measured
    per request in the gateway and never crosses into the control plane.
    """

    id: str
    endpoint: str
    endpoint_name: str
    project: str
    committed_rpm: int | None
    committed_tpm: int | None
    max_concurrent_requests: int | None
    max_output_tokens: int | None
    burst_rpm: int | None
    ramp_up_seconds: int
    on_exceed: str
    queue_timeout_ms: int | None
    limit_scope: str
    success_rate_objective: float | None
    availability_objective: str
    objective_window_seconds: int
    enforced: bool
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str
    standard: ServingQuotaStandardSchema


class ServingQuotaCreateRequest(Struct):
    """Never auto-created. A serving quota is a number somebody sold."""

    endpoint: str
    committed_rpm: int | None = None
    committed_tpm: int | None = None
    max_concurrent_requests: int | None = None
    max_output_tokens: int | None = None
    burst_rpm: int | None = None
    ramp_up_seconds: int = 0
    on_exceed: str = "reject"
    queue_timeout_ms: int | None = None
    limit_scope: str = "per_replica"
    success_rate_objective: float | None = None
    availability_objective: str = DEFAULT_AVAILABILITY_OBJECTIVE
    objective_window_seconds: int = 600
    enforced: bool = False


class ServingQuotaUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `endpoint` is absent by design, exactly as `project` is on `QuotaUpdateRequest`:
    the endpoint is this row's whole scope and the FK is unique, so moving one is
    withdrawing a commitment from one contract and granting it to another.
    """

    committed_rpm: int | None | UnsetType = UNSET
    committed_tpm: int | None | UnsetType = UNSET
    max_concurrent_requests: int | None | UnsetType = UNSET
    max_output_tokens: int | None | UnsetType = UNSET
    burst_rpm: int | None | UnsetType = UNSET
    ramp_up_seconds: int | UnsetType = UNSET
    on_exceed: str | UnsetType = UNSET
    queue_timeout_ms: int | None | UnsetType = UNSET
    limit_scope: str | UnsetType = UNSET
    success_rate_objective: float | None | UnsetType = UNSET
    availability_objective: str | UnsetType = UNSET
    objective_window_seconds: int | UnsetType = UNSET
    enforced: bool | UnsetType = UNSET


class ResolvedServingQuotaSchema(Struct):
    """What one endpoint's commitment means for a fleet of a stated size.

    `declared` is what was sold across the whole gateway fleet; `effective` is
    the share of it the replica at `replica_index` would hold once `limit_scope` is
    applied. No gateway reads either yet.
    Under `per_replica` the two are equal by construction and every replica
    admits the whole number, so a fleet of R admits up to R times what was sold.

    `workloads` is a lookup convenience and never a key: the commitment belongs
    to the endpoint, and a workload appears here because traffic reaching it was
    admitted through this endpoint's name. A workload behind two endpoints has
    two commitments and they are reported separately, not summed: summing
    them would invent a number nobody sold.
    """

    endpoint: str
    endpoint_name: str
    project: str
    workloads: list[str]
    replicas: int
    replica_index: int
    declared: ResolvedLimitsSchema
    effective: ResolvedLimitsSchema
    starved_replicas: int
    success_rate_objective: float | None
    availability_objective: str
    objective_window_seconds: int
    standard: ServingQuotaStandardSchema
    revision: str


class ResolvedServingQuotaTableSchema(Struct):
    """Every enabled endpoint that carries a commitment, resolved for one fleet.

    Unpaginated, like the routing table it sits beside: a caller that assembled
    half of it would enforce half its commitments. `revision` covers the quota
    rows, the endpoints they commit, the targets behind them and the fleet the
    resolution was asked for, so a caller that polls can send it back as
    `If-None-Match` and pay a `304` for as long as none of the four have moved.
    """

    revision: str
    replicas: int
    replica_index: int
    quotas: list[ResolvedServingQuotaSchema]
