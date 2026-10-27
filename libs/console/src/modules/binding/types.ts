/**
 * `ProjectTenantSchema`, embedded instead of linked: a project always carries
 * the tenant it belongs to, even though `/v1/tenants` now exists to make one.
 */
/**
 * `TenantSchema`: the billing and isolation root a project hangs off.
 *
 * `project_count` is on the read shape because the only destructive question
 * anyone asks of a tenant is what deleting it would take with it.
 */
export type TenantIngress = "internal" | "public"

export type TenantAuth = "none" | "api_key"

export interface Tenant {
  id: string
  slug: string
  name: string
  enabled: boolean
  /** Which listener this tenant's traffic arrives on (docs/decisions/0016). */
  ingress: TenantIngress
  /** Whether the gateway checks a key on this tenant's requests. */
  auth: TenantAuth
  /** The name that resolves to this tenant, checked against `Host`. Null until set. */
  host: string | null
  project_count: number
  created_at: string
  updated_at: string | null
  revision: string
}

export interface TenantCreateRequest {
  slug: string
  name: string
  enabled?: boolean
  ingress?: TenantIngress
  auth?: TenantAuth
  host?: string | null
}

/** The slug namespaces every project beneath it, so it is not patchable. */
export interface TenantUpdateRequest {
  name?: string
  enabled?: boolean
  ingress?: TenantIngress
  auth?: TenantAuth
  host?: string | null
}

/**
 * `TenantAPIKeySchema`: a gateway inference credential, distinct from the
 * console sign-in token: this one is what a caller on the tenant's public
 * listener presents, not what signs in here.
 */
export interface TenantAPIKey {
  id: string
  tenant: string
  name: string
  /** Present only on the mint response: shown once, never returned by a list. */
  key: string | null
  last_used_at: string | null
  revoked_at: string | null
  created_at: string
}

export interface TenantAPIKeyCreateRequest {
  name?: string
}

export interface ProjectTenant {
  id: string
  slug: string
  name: string
  enabled: boolean
}

/** `ProjectSchema`: the tenancy unit quota attaches to. */
export interface Project {
  id: string
  tenant: ProjectTenant
  slug: string
  name: string
  default_priority_class: string | null
  enabled: boolean
  /** Both switches folded for scheduling, without losing which one is off. */
  effective_enabled: boolean
  created_at: string
  updated_at: string | null
  revision: string
}

/** The tenant is a slug or id, picked from `/v1/tenants`. */
export interface ProjectCreateRequest {
  tenant: string
  slug: string
  name: string
  default_priority_class?: string | null
  enabled?: boolean
}

/** The slug is identity and the tenant is ownership, so neither is patchable. */
export interface ProjectUpdateRequest {
  name?: string
  default_priority_class?: string | null
  enabled?: boolean
}

/** `QuotaUsageSchema`: the observed half, counted over live allocations. */
export interface QuotaUsage {
  allocated_devices: number
  live_allocations: number
  over_quota_devices: number
}

/** `QuotaSchema`: a guaranteed floor and a fair share, usage beside it. */
export interface Quota {
  id: string
  project: string
  project_slug: string
  /** Null when the quota spans every cluster; usage is then summed across all. */
  cluster: string | null
  cluster_name: string | null
  device_class: string
  guaranteed_devices: number
  limit_devices: number | null
  over_quota_weight: number
  over_quota_enabled: boolean
  created_at: string
  updated_at: string | null
  revision: string
  usage: QuotaUsage
  headroom_devices: number | null
  over_limit: boolean
}

/** Project, cluster and class fix what the quota is about, and only at creation. */
export interface QuotaCreateRequest {
  project: string
  device_class: string
  cluster?: string | null
  guaranteed_devices?: number
  limit_devices?: number | null
  over_quota_weight?: number
  over_quota_enabled?: boolean
}

export interface QuotaUpdateRequest {
  guaranteed_devices?: number
  limit_devices?: number | null
  over_quota_weight?: number
  over_quota_enabled?: boolean
}

export type LimitScope = "per_replica" | "divided"

export type OnExceed = "reject" | "queue"

/**
 * `ServingQuotaStandardSchema`: whether a declared commitment clears the
 * acceptance standard's floors. Declared against declared: it compares the
 * numbers somebody promised with the numbers the standard asks to be promised,
 * and says nothing about what the endpoint delivered. An uncommitted field is a
 * shortfall, because declaring nothing does not clear a floor.
 */
export interface ServingQuotaStandard {
  meets: boolean
  shortfalls: string[]
}

/**
 * `ServingQuotaSchema`: the traffic one endpoint is committed to carry.
 *
 * Not a `Quota`. That one is devices, checked once by placement when a workload
 * is admitted; this one is requests and tokens, checked by the gateway on every
 * request, in front of the clusters. Different enforcer, different moment, and
 * no field in common.
 *
 * Every field is declared, and there is intentionally no observed sibling of
 * `Quota.usage`: a serving quota is counted per request in the gateway, and that
 * measurement never crosses into the control plane.
 *
 * A null limit is uncommitted and never zero. An endpoint with no row at all is
 * not rate limited, and a row with a null `committed_rpm` is committed on tokens
 * and not on requests.
 */
export interface ServingQuota {
  id: string
  endpoint: string
  endpoint_name: string
  project: string
  committed_rpm: number | null
  committed_tpm: number | null
  max_concurrent_requests: number | null
  max_output_tokens: number | null
  burst_rpm: number | null
  ramp_up_seconds: number
  on_exceed: OnExceed
  queue_timeout_ms: number | null
  limit_scope: LimitScope
  success_rate_objective: number | null
  availability_objective: string
  objective_window_seconds: number
  /** False is shadow mode: the gateway counts and reports, and never refuses. */
  enforced: boolean
  created_at: string
  updated_at: string | null
  revision: string
  standard: ServingQuotaStandard
}

/** Never auto-created: a serving quota is a number somebody sold. */
export interface ServingQuotaCreateRequest {
  endpoint: string
  committed_rpm?: number | null
  committed_tpm?: number | null
  max_concurrent_requests?: number | null
  max_output_tokens?: number | null
  burst_rpm?: number | null
  ramp_up_seconds?: number
  on_exceed?: OnExceed
  queue_timeout_ms?: number | null
  limit_scope?: LimitScope
  success_rate_objective?: number | null
  availability_objective?: string
  objective_window_seconds?: number
  enforced?: boolean
}

/**
 * An absent field is untouched and an explicit null clears it. `endpoint` is
 * absent by design: the endpoint is this row's whole scope, so moving one would
 * withdraw a commitment from one contract and grant it to another.
 */
export interface ServingQuotaUpdateRequest {
  committed_rpm?: number | null
  committed_tpm?: number | null
  max_concurrent_requests?: number | null
  max_output_tokens?: number | null
  burst_rpm?: number | null
  ramp_up_seconds?: number
  on_exceed?: OnExceed
  queue_timeout_ms?: number | null
  limit_scope?: LimitScope
  success_rate_objective?: number | null
  availability_objective?: string
  objective_window_seconds?: number
  enforced?: boolean
}

/**
 * `ResolvedLimitsSchema`: one endpoint's ceiling, flattened for the gateway.
 * `limit_scope` travels with the numbers because it is how they are read:
 * `per_replica` means they are this replica's, `divided` means they are the
 * fleet's and this replica enforces its share.
 */
export interface ResolvedLimits {
  committed_rpm: number | null
  burst_rpm: number | null
  ramp_up_seconds: number
  committed_tpm: number | null
  max_concurrent_requests: number | null
  max_output_tokens: number | null
  on_exceed: OnExceed
  queue_timeout_ms: number | null
  limit_scope: LimitScope
  enforced: boolean
}

/**
 * `ResolvedServingQuotaSchema`: what a commitment means for a fleet of a
 * stated size. `declared` is what was sold across the whole gateway fleet;
 * `effective` is what the replica at `replica_index` enforces once `limit_scope`
 * is applied. The plane does this arithmetic, so the console never computes a
 * second opinion of it.
 */
export interface ResolvedServingQuota {
  endpoint: string
  endpoint_name: string
  project: string
  workloads: string[]
  replicas: number
  replica_index: number
  declared: ResolvedLimits
  effective: ResolvedLimits
  /** How many replicas would enforce a hard zero on some limit under `divided`. */
  starved_replicas: number
  success_rate_objective: number | null
  availability_objective: string
  objective_window_seconds: number
  standard: ServingQuotaStandard
  revision: string
}

/** Unpaginated, like the routing table it sits beside. */
export interface ResolvedServingQuotaTable {
  revision: string
  replicas: number
  replica_index: number
  quotas: ResolvedServingQuota[]
}

/**
 * `PriorityClassSchema`: what preempts what. `preemptible` and `preempts` are
 * separate switches: a class can take capacity from others and still be safe
 * from eviction itself.
 */
export interface PriorityClass {
  id: string
  name: string
  value: number
  preemptible: boolean
  preempts: boolean
  is_default: boolean
  description: string | null
  created_at: string
  updated_at: string | null
  revision: string
}

export interface PriorityClassCreateRequest {
  name: string
  value: number
  preemptible?: boolean
  preempts?: boolean
  is_default?: boolean
  description?: string | null
}

/** The name is what workloads reference by string, so it is fixed at creation. */
export interface PriorityClassUpdateRequest {
  value?: number
  preemptible?: boolean
  preempts?: boolean
  is_default?: boolean
  description?: string | null
}

/**
 * `PolicyAttachmentSchema`. `subject_id` is the raw polymorphic key and is
 * carried beside `subject` so a deleted subject still serializes with identity.
 */
export interface PolicyAttachment {
  subject_type: string
  subject_id: string
  subject: string | null
  subject_name: string | null
}

/**
 * `PolicySchema`: a placement constraint and everything it binds to.
 * Allow-lists and deny-lists are separate: an empty allow-list means "no
 * opinion", not "nothing allowed".
 */
export interface Policy {
  id: string
  name: string
  description: string | null
  allowed_regions: string[]
  allowed_residencies: string[]
  allowed_device_classes: string[]
  allowed_clusters: string[]
  denied_clusters: string[]
  rules: Record<string, unknown>
  enabled: boolean
  created_at: string
  updated_at: string | null
  revision: string
  attachments: PolicyAttachment[]
}

/** `PolicyAttachmentRequest`: `subject` is a ref, resolved against its type. */
export interface PolicyAttachmentRequest {
  subject_type: string
  subject: string
}

export interface PolicyCreateRequest {
  name: string
  description?: string | null
  allowed_regions?: string[]
  allowed_residencies?: string[]
  allowed_device_classes?: string[]
  allowed_clusters?: string[]
  denied_clusters?: string[]
  rules?: Record<string, unknown>
  enabled?: boolean
  attachments?: PolicyAttachmentRequest[]
}

export interface PolicyUpdateRequest {
  name?: string
  description?: string | null
  allowed_regions?: string[]
  allowed_residencies?: string[]
  allowed_device_classes?: string[]
  allowed_clusters?: string[]
  denied_clusters?: string[]
  rules?: Record<string, unknown>
  enabled?: boolean
  attachments?: PolicyAttachmentRequest[]
}
