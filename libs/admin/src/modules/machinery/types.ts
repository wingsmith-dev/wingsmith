/**
 * `OperationTargetSchema`. The kind is carried explicitly rather than read off
 * the id prefix — a client that parses ids to know what it is looking at is a
 * client that will eventually parse one wrong.
 */
export interface OperationTarget {
  type: string
  id: string
}

/** Every operation type the plane accepts, and the id prefix its target carries. */
export const OPERATION_TYPES = {
  "node.drain": "nod_",
  "node.cordon": "nod_",
  "node.uncordon": "nod_",
  "node.reboot": "nod_",
  "device.reset": "dev_",
  "cache.warm": "art_",
  "deployment.roll": "dep_",
  "component.upgrade": "cmp_",
} as const

export type OperationType = keyof typeof OPERATION_TYPES

/** The kind of object each operation type acts upon, mirroring the plane. */
export const OPERATION_TARGET_KINDS = {
  "node.drain": "node",
  "node.cordon": "node",
  "node.uncordon": "node",
  "node.reboot": "node",
  "device.reset": "device",
  "cache.warm": "artifact",
  "deployment.roll": "deployment",
  "component.upgrade": "component",
} as const satisfies Record<OperationType, string>

const ID_PATTERN = /^[a-z]{3}_[0-9a-z]{26}$/

/**
 * Turns what a human typed into a target the plane can read a kind off.
 *
 * A target reference has to say what kind of thing it names — that is what
 * makes `node.drain` against a device an error at creation rather than inside
 * the cluster an hour later. An id says it in the prefix; a name says nothing,
 * so the type's own target kind is written in front of it.
 */
export function qualifyTarget(type: OperationType, target: string): string {
  const trimmed = target.trim()
  if (trimmed === "" || ID_PATTERN.test(trimmed) || trimmed.includes(":"))
    return trimmed
  return `${OPERATION_TARGET_KINDS[type]}:${trimmed}`
}

/** Whether a typed target names a kind this operation type does not act upon. */
export function targetMismatch(type: OperationType, target: string): boolean {
  const trimmed = target.trim()
  if (trimmed === "") return false
  if (ID_PATTERN.test(trimmed))
    return !trimmed.startsWith(OPERATION_TYPES[type])
  const [kind, name] = trimmed.split(":")
  if (name === undefined) return false
  return kind !== OPERATION_TARGET_KINDS[type] || name === ""
}

export type OperationPhase =
  "pending" | "running" | "succeeded" | "failed" | "cancelled" | "timed_out"

export const OPEN_PHASES: readonly OperationPhase[] = ["pending", "running"]

export function isOpen(phase: string): boolean {
  return (OPEN_PHASES as readonly string[]).includes(phase)
}

/**
 * `OperationSchema` — an imperative action with a lifecycle, not a verb that
 * returned 200. The declared half is written once at creation; phase, result,
 * error, attempts and the timestamps are ingestion's thereafter.
 */
export interface Operation {
  id: string
  cluster_id: string | null
  type: OperationType | (string & {})
  target: OperationTarget
  reason: string | null
  requested_by: string
  parameters: Record<string, unknown>
  deadline_at: string | null
  created_at: string
  updated_at: string | null
  phase: OperationPhase | (string & {})
  result: Record<string, unknown> | null
  error: string | null
  attempts: number
  cancel_requested_at: string | null
  started_at: string | null
  finished_at: string | null
}

/** `POST /v1/operations` — a type and a target, never a verb on the target. */
export interface OperationCreateRequest {
  type: string
  target: string
  reason?: string | null
  deadline?: string | null
  parameters?: Record<string, unknown>
  cluster?: string | null
}

/** `ComponentStatusSchema` — the observed half; its single writer is ingestion. */
export interface ComponentStatus {
  observed_version: string | null
  rollout_phase: string
  healthy: boolean
  message: string | null
  observed_at: string | null
}

/**
 * `ComponentSchema` — declared version and observed version as a pair, never
 * one nullable column. `behind` is derived by the plane, and a component nobody
 * has reported on counts as behind: silence is not a finished rollout.
 */
export interface Component {
  id: string
  cluster_id: string
  name: string
  kind: string
  desired_version: string
  config: Record<string, unknown>
  enabled: boolean
  created_at: string
  updated_at: string | null
  revision: string
  status: ComponentStatus
  behind: boolean
}

/**
 * `EventSubjectSchema`. `id` is the subject's row identifier and is **not** a
 * prefixed public id — `Event.subject_id` is an untyped UUID.
 */
export interface EventSubject {
  type: string
  id: string | null
}

/**
 * `EventSchema` — one observed, append-only state record. No declared half and
 * no revision: nothing declares an event and nothing may edit one. `id` is an
 * `evt_` public identifier, not the row's primary key.
 */
export interface PlaneEvent {
  id: string
  cluster_id: string | null
  kind: string
  severity: string
  subject: EventSubject
  operation_id: string | null
  message: string
  details: Record<string, unknown>
  occurred_at: string
  recorded_at: string
}

/** `AllocationDeviceSchema`. Released members are returned, not filtered out. */
export interface AllocationDevice {
  device: string
  device_uuid: string
  node: string
  live: boolean
  released_at: string | null
}

/**
 * `AllocationSchema` — a set of devices committed together to one workload.
 * Every field is written by ingestion; `gang_complete` is the one derived one,
 * because holding fewer live devices than `device_count` is a partially
 * satisfied gang and that differs from being released.
 */
export interface Allocation {
  id: string
  cluster: string
  cluster_name: string
  project: string
  project_slug: string
  workload: string
  replica: string | null
  device_class: string
  device_count: number
  over_quota: boolean
  phase: string
  live: boolean
  gang_complete: boolean
  acquired_at: string | null
  released_at: string | null
  observed_at: string | null
  created_at: string
  updated_at: string | null
  devices: AllocationDevice[]
}
