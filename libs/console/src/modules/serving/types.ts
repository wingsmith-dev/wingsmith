/** What a tenant is served, as the tenant is allowed to see it. */
export interface ServedModel {
  id: string
  /** The plane's internal handle. Shown small, beside the model id, for support. */
  name: string
  /** The string a client sends as `model`. This is the name that matters here. */
  model: string
  state: ServedModelState
  replicas_ready: number
  replicas_desired: number
  created_at: string
}

/**
 * `paused` is a decision somebody made and `degraded` is not. Keeping them apart
 * is the difference between a status page and an alarm.
 */
export type ServedModelState = "serving" | "degraded" | "paused"

export interface ServingLimits {
  requests_per_minute: number | null
  tokens_per_minute: number | null
  max_concurrent_requests: number | null
  enforced: boolean
}

export interface ServedModelDetail {
  model: ServedModel
  limits: ServingLimits | null
  protocols: string[]
}

export interface ServingOverview {
  tenant: string
  tenant_name: string
  models: ServedModel[]
}
