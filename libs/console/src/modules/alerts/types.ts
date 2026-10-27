/**
 * Mirrors `app/modules/alerts/schemas.py`. Two objects of different kinds:
 * an alert is derived and nothing declares one, a threshold is the number a
 * person committed to and nothing else may write it.
 */

/**
 * What the alert or the threshold is about. `id` may name something that no
 * longer exists: an alert about a node that went away has to outlive the node.
 */
export interface AlertSubject {
  type: string
  id: string | null
}

/**
 * Where the alert is in its life, which is a different axis from severity.
 * `pending` is true but not yet for long enough to be worth telling anyone, and
 * is intentionally visible here and notified nowhere.
 */
export type AlertState = "pending" | "firing" | "acknowledged" | "resolved"

export type AlertSeverity = "info" | "warning" | "error" | "critical"

/** Conditions are level-triggered and resolve themselves; events never do. */
export type AlertSource = "condition" | "event"

export interface Alert {
  id: string
  rule: string
  source: AlertSource | (string & {})
  severity: AlertSeverity | (string & {})
  state: AlertState | (string & {})
  title: string
  cluster_id: string | null
  subject: AlertSubject
  details: Record<string, unknown>
  occurrences: number
  first_seen_at: string
  last_seen_at: string
  fired_at: string | null
  resolved_at: string | null
  acknowledged_at: string | null
  acknowledged_by: string | null
  acknowledged_note: string | null
  notified_at: string | null
  notify_error: string | null
}

/** There is no `by`: the acknowledger is the request principal. */
export interface AlertAcknowledgeRequest {
  note?: string | null
}

/**
 * `MetricRuleSchema`: one entry in the catalog of rules a threshold may
 * name. `metric` is a name from the metrics registry, never an expression, so
 * a form built from this offers registry names and nothing else.
 */
export interface MetricRule {
  name: string
  metric: string
  unit: string
  comparison: "above" | "below" | (string & {})
  subject_type: string
  severity: AlertSeverity | (string & {})
  measures: string
  suggested_value: number
  lookback: string
  for_seconds: number
}

/**
 * `AlertThresholdSchema`. `subject.id` of null is the fleet default for that
 * rule; a row naming a subject overrides it for that subject alone.
 *
 * `lookback`, `for_seconds` and `severity` come back exactly as declared, null
 * included, because null means "whatever the rule says": the catalog carries
 * the fallback and this shape must not pretend the row pinned one.
 */
export interface AlertThreshold {
  id: string
  rule: string
  metric: string
  comparison: "above" | "below" | (string & {})
  subject: AlertSubject
  dimensions: Record<string, string>
  value: number
  objective: string | null
  lookback: string | null
  for_seconds: number | null
  severity: AlertSeverity | (string & {}) | null
  enabled: boolean
  reason: string | null
  created_at: string
  updated_at: string | null
}

/** `comparison` is absent by design: which way a metric is wrong is not declarable. */
export interface AlertThresholdCreateRequest {
  rule: string
  value: number
  subject_type?: string | null
  subject_id?: string | null
  dimensions?: Record<string, string>
  objective?: string | null
  lookback?: string | null
  for_seconds?: number | null
  severity?: string | null
  enabled?: boolean
  reason?: string | null
}

/**
 * A partial update. `rule`, `subject_type` and `subject_id` are the row's
 * identity and are not patchable: moving a commitment onto another rule or
 * another subject is the creation of a different commitment.
 */
export interface AlertThresholdUpdateRequest {
  value?: number
  dimensions?: Record<string, string>
  objective?: string | null
  lookback?: string | null
  for_seconds?: number | null
  severity?: string | null
  enabled?: boolean
  reason?: string | null
}
