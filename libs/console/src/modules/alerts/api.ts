import { api, type ListResponse } from "@/lib/api"
import type {
  Alert,
  AlertAcknowledgeRequest,
  AlertThreshold,
  AlertThresholdCreateRequest,
  AlertThresholdUpdateRequest,
  MetricRule,
} from "./types"

export interface AlertFilters {
  /** Sent as `state`. */
  state?: string
  severity?: string
  rule?: string
  cluster?: string
  subject?: string
  since?: string
}

export interface AlertThresholdFilters {
  rule?: string
  subject_type?: string
  subject?: string
  enabled?: boolean
  /** Only the rows with no subject: the fleet defaults. */
  fleet_default?: boolean
}

export function fetchAlerts(
  filters: AlertFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<Alert>> {
  return api.list<Alert>("/alerts", { ...filters, cursor, limit })
}

/**
 * Silences an alert without claiming it is over. A `POST` to a sub-path, not a
 * `PATCH` of `state`: it is a transition with preconditions, not a field
 * assignment, and nothing about the derived alert itself is editable.
 */
export function acknowledgeAlert(
  ref: string,
  body: AlertAcknowledgeRequest = {}
): Promise<Alert> {
  return api.post<Alert>(`/alerts/${encodeURIComponent(ref)}/acknowledge`, body)
}

/**
 * Closes an alert by hand. On a condition-sourced alert this is a snooze: the
 * reactor recomputes from scratch, so a condition still true reopens it.
 */
export function resolveAlert(ref: string): Promise<Alert> {
  return api.post<Alert>(`/alerts/${encodeURIComponent(ref)}/resolve`, {})
}

export function fetchAlertThresholds(
  filters: AlertThresholdFilters = {},
  cursor?: string,
  limit?: number
): Promise<ListResponse<AlertThreshold>> {
  return api.list<AlertThreshold>("/alert-thresholds", {
    ...filters,
    cursor,
    limit,
  })
}

export function createAlertThreshold(
  body: AlertThresholdCreateRequest
): Promise<AlertThreshold> {
  return api.post<AlertThreshold>("/alert-thresholds", body)
}

/** No `If-Match`: nothing but a person writes this row and there is no revision. */
export function updateAlertThreshold(
  ref: string,
  body: AlertThresholdUpdateRequest
): Promise<AlertThreshold> {
  return api.patch<AlertThreshold>(
    `/alert-thresholds/${encodeURIComponent(ref)}`,
    body
  )
}

export function deleteAlertThreshold(ref: string): Promise<void> {
  return api.delete<void>(`/alert-thresholds/${encodeURIComponent(ref)}`)
}

/**
 * The catalog of rules a threshold may name. Not paginated and not a table:
 * it is derived from the compiled predicates, and a console that cannot list it
 * has to hardcode metric names.
 */
export function fetchAlertRules(): Promise<ListResponse<MetricRule>> {
  return api.list<MetricRule>("/alert-rules")
}
