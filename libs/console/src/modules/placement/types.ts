/**
 * `POST /v1/placement/preview` and `POST /v1/placement/expand` — the plane's own
 * answer to "where would this go", asked without creating anything — and
 * `GET /v1/placement/reservations`, which answers "what is about to arrive here".
 *
 * These are the authority and the console's arithmetic is not. The board can see
 * devices, fabrics and health; only the plane can see quota, priority classes,
 * policies, residency and what would have to be preempted, and it is the same
 * code path a real create runs. Anything derived here that disagrees with a
 * decision is wrong by definition.
 */

/** `Rejection.code`. Left open: the plane may add a reason we have not been taught. */
export type RejectionCode =
  | "cluster_phase"
  | "cluster_unreachable"
  | "policy_region"
  | "policy_residency"
  | "policy_device_class"
  | "policy_cluster_not_allowed"
  | "policy_cluster_denied"
  | "quota_exhausted"
  | "no_such_device_class"
  | "insufficient_capacity"
  | "insufficient_topology"
  | "name_taken"
  | (string & {})

/** One reason a cluster was ruled out, with the numbers it was computed from. */
export interface Rejection {
  code: RejectionCode
  message: string
  detail: Record<string, unknown>
}

/**
 * A workload that would have to go for this placement to fit.
 *
 * `devices_in_scope` is what evicting it contributes here; `devices_released` is
 * everything it holds in the cluster. They differ when a workload straddles
 * topology domains, and the difference is the honest cost — an allocation dies
 * whole, so devices outside the scope are freed too.
 */
export interface PreemptionVictim {
  workload: string
  project: string
  priority_value: number
  devices_in_scope: number
  devices_released: number
  deployment: string | null
}

/** What would be evicted. Identified by placement, never performed by it. */
export interface PreemptionPlan {
  victims: PreemptionVictim[]
  devices_reclaimed: number
}

/**
 * One cluster's full reasoning, feasible or not.
 *
 * The counts come back even on a rejection, which is the point: "6 free h100 in
 * the largest nvlink domain, 8 asked for" is a next action and "no capacity" is
 * not.
 */
export interface ClusterVerdict {
  cluster: string
  cluster_name: string
  feasible: boolean
  free_devices: number
  /**
   * Devices this cluster already owes a deployment that has not bound them yet.
   *
   * Reported beside `free_devices` rather than folded into it, because the two
   * are different kinds of fact: free is observed, reserved is promised. An
   * operator told "0 free" while eight accelerators sit visibly idle has been
   * told something untrue, and the reason the fit was refused is the number that
   * was never on the screen.
   */
  reserved_devices: number
  /** `free_devices` net of `reserved_devices`. The number every fit is decided on. */
  available_devices: number
  reasons: Rejection[]
  domain: string | null
  domain_identifier: string | null
  domain_free_devices: number
  largest_domain_free_devices: number
  fit_slack: number
  /** Null when no quota is declared for the project — not the same as zero. */
  quota_headroom: number | null
  preemption: PreemptionPlan | null
}

/** Which cluster one replica would go to, and why every other one lost. */
export interface PlacementDecision {
  request: string
  placed: boolean
  cluster: string | null
  cluster_name: string | null
  verdicts: ClusterVerdict[]
  preemption: PreemptionPlan | null
  summary: string
}

/**
 * One cluster's slice of a workload's total scale, counted in the workload's own
 * unit. Always this cluster's share and never the total.
 */
export interface Share {
  cluster: string
  cluster_name: string
  replicas: number
}

/**
 * How a workload's scale was spread over clusters, and why.
 *
 * `shortfall` is a partial placement rather than a failure — the shares found are
 * real. `splittable` is false for a gang, whose ranks are co-scheduled: one that
 * fits nowhere whole comes back with no shares and the whole gang as shortfall,
 * and there is no degraded answer to fall back on.
 */
export interface ExpansionDecision {
  workload: string
  shares: Share[]
  verdicts: ClusterVerdict[]
  desired: number
  shortfall: number
  splittable: boolean
  summary: string
}

export interface PlacementPreviewRequest {
  recipe: string
  project: string
  priority_class?: string | null
  replicas?: number
}

/**
 * `ReservationSchema` — capacity a cluster owes a deployment that has not bound
 * a device yet.
 *
 * Derived by the plane on every read and stored nowhere: it is the difference
 * between what a deployment declares and what ingestion has confirmed the
 * cluster actually bound, so it appears the moment placement chooses a cluster
 * and decays to zero on its own as the allocations arrive.
 *
 * It is a count with a shape and no device address: which accelerators the
 * promise lands on is the cluster's answer and it has not given it yet — rule 3,
 * device selection is local. The board still paints cells for it, because a
 * cluster reading "0 available" beside eight visibly idle squares and nothing to
 * point at is the confusion this endpoint exists to remove. Those cells are the
 * board's own plausible guess and everything rendering them has to say so; the
 * number here is the only part that is exact.
 */
export interface Reservation {
  cluster: string
  cluster_name: string
  project: string
  workload: string
  workload_name: string | null
  deployment: string | null
  deployment_name: string | null
  device_class: string
  /** Devices still owed. Never the deployment's whole declared size. */
  devices: number
  requires_single_domain: boolean
  domain_kind: string | null
}
