/**
 * `ProjectTenantSchema` — embedded rather than linked: a project always carries
 * the tenant it belongs to, even though `/v1/tenants` now exists to make one.
 */
/**
 * `TenantSchema` — the billing and isolation root a project hangs off.
 *
 * `project_count` is on the read shape because the only destructive question
 * anyone asks of a tenant is what deleting it would take with it.
 */
export interface Tenant {
  id: string
  slug: string
  name: string
  enabled: boolean
  project_count: number
  created_at: string
  updated_at: string | null
  revision: string
}

export interface TenantCreateRequest {
  slug: string
  name: string
  enabled?: boolean
}

/** The slug namespaces every project beneath it, so it is not patchable. */
export interface TenantUpdateRequest {
  name?: string
  enabled?: boolean
}

export interface ProjectTenant {
  id: string
  slug: string
  name: string
  enabled: boolean
}

/** `ProjectSchema` — the tenancy unit `X-Hangar-Project` names. */
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

/** `QuotaUsageSchema` — the observed half, counted over live allocations. */
export interface QuotaUsage {
  allocated_devices: number
  live_allocations: number
  over_quota_devices: number
}

/** `QuotaSchema` — a guaranteed floor and a fair share, usage beside it. */
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

/**
 * `PriorityClassSchema` — what preempts what. `preemptible` and `preempts` are
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
 * carried beside `subject` so a deleted subject still serialises with identity.
 */
export interface PolicyAttachment {
  subject_type: string
  subject_id: string
  subject: string | null
  subject_name: string | null
}

/**
 * `PolicySchema` — a placement constraint and everything it binds to.
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

/** `PolicyAttachmentRequest` — `subject` is a ref, resolved against its type. */
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
