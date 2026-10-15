/**
 * Every polling cadence in the console, in one place so they can be compared. Each
 * module names the one it uses and says why at the call site.
 */
export const POLL = {
  /** An unfinished operation. */
  operation: 4_000,
  /** A deployment coming up, or an unfinished benchmark run. */
  progress: 5_000,
  /** Allocations and reservations. The two are read side by side and must match. */
  allocations: 10_000,
  /** The alert list. */
  alerts: 15_000,
  /** Measurements, and the tenant's status page. */
  metrics: 30_000,
} as const
