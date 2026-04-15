import { OPERATION_TYPES, type OperationType } from "./types"

/**
 * What is known about the object an operation would act on. Structural rather
 * than typed against a domain object: the registry lives in machinery and must
 * not learn what a node is.
 */
export interface OperationContext {
  /** Types already open against this target — a second drain is not a drain. */
  open?: readonly string[]
  cordoned?: boolean
  allocated?: boolean
  paused?: boolean
}

/** Returns why the operation does not apply, or null when it does. */
type Predicate = (context: OperationContext) => string | null

const LABELS: Partial<Record<OperationType, string>> = {
  "cache.warm": "Warm cache",
  "component.upgrade": "Upgrade",
  "deployment.roll": "Roll",
  "workload.scale": "Scale",
  "workload.pause": "Pause",
  "workload.drain": "Drain a cluster",
}

const PREDICATES: Partial<Record<OperationType, Predicate>> = {
  "node.drain": (context) => (context.cordoned ? "already cordoned" : null),
  "node.cordon": (context) => (context.cordoned ? "already cordoned" : null),
  "node.uncordon": (context) =>
    context.cordoned === false ? "not cordoned" : null,
  "node.reboot": (context) =>
    context.cordoned === false ? "cordon the node first" : null,
  "device.reset": (context) =>
    context.allocated ? "a live allocation holds this device" : null,
  "workload.pause": (context) => (context.paused ? "already paused" : null),
}

function derivedLabel(type: string): string {
  const verb = type.slice(type.indexOf(".") + 1)
  return verb.charAt(0).toUpperCase() + verb.slice(1)
}

export interface OperationDefinition {
  type: OperationType
  label: string
  /** The id prefix its target carries, taken straight off `OPERATION_TYPES`. */
  prefix: string
  applies: Predicate
}

/**
 * Every operation type, derived from `OPERATION_TYPES` rather than listed a
 * second time. A new entry there surfaces here on its own with a label read off
 * the verb and no predicate — the honest default for an operation whose state
 * rule nobody has written yet.
 */
export const OPERATION_REGISTRY = Object.fromEntries(
  Object.entries(OPERATION_TYPES).map(([type, prefix]) => [
    type,
    {
      type: type as OperationType,
      prefix,
      label: LABELS[type as OperationType] ?? derivedLabel(type),
      applies: PREDICATES[type as OperationType] ?? (() => null),
    } satisfies OperationDefinition,
  ])
) as Record<OperationType, OperationDefinition>

/** Every operation whose target carries this id prefix, in declaration order. */
export function operationsForPrefix(prefix: string): OperationDefinition[] {
  return Object.values(OPERATION_REGISTRY).filter(
    (entry) => entry.prefix === prefix
  )
}

export function operationLabel(type: string): string {
  return OPERATION_REGISTRY[type as OperationType]?.label ?? derivedLabel(type)
}

export interface Applicability {
  enabled: boolean
  reason: string | null
}

/**
 * Applicability is state-dependent and the answer is never to hide the action:
 * a missing button reads as a missing feature, a disabled one with its reason
 * teaches the state machine. An open operation of the same type disqualifies
 * every type uniformly, so no predicate has to remember to check for it.
 */
export function applicability(
  type: OperationType,
  context: OperationContext
): Applicability {
  const definition = OPERATION_REGISTRY[type]
  const reason = context.open?.includes(type)
    ? "already in flight"
    : (definition?.applies(context) ?? null)
  return { enabled: reason === null, reason }
}
