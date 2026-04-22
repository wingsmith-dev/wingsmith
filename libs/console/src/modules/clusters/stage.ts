import type { StatusTone } from "@/components/layout/status-dot"
import type { Cluster } from "./types"

/**
 * The lifecycle stage a cluster is *in*, derived from declared phase and
 * observed link — the single fact the detail page switches its whole layout on.
 * Distinct from `phase` (declared intent) and `sync.state` (observed link):
 * a stage folds both into "what is this cluster's operator, right now, and so
 * what is worth showing a human".
 *
 * - `onboarding` — declared, but the operator has never dialled in. There is no
 *   observed state yet, so the only thing worth showing is how to enrol it.
 * - `live` — enrolled, reachable, reporting. The full operational dashboard.
 * - `degraded` — enrolled once, but the link is now stale, dropped, or the
 *   cluster is unreachable. Last-known observed state still stands; it is just
 *   not fresh, so we show it under a warning rather than hide it.
 * - `draining` — being decommissioned. Progress first, dashboard read-only.
 * - `archived` — detached/disabled. Read-only identity and history, no actions.
 */
export type ClusterStage =
  "onboarding" | "live" | "degraded" | "draining" | "archived"

/**
 * Order matters: `disabled` is terminal (archived even if it never connected);
 * a cluster that never connected is onboarding before anything else; draining
 * outranks a degraded link because the drain is the story; a bad link outranks
 * `live`. Everything else is live.
 */
export function clusterStage(cluster: Cluster): ClusterStage {
  if (cluster.phase === "disabled") return "archived"
  if (cluster.sync.state === "never_connected") return "onboarding"
  if (cluster.phase === "draining") return "draining"
  if (
    !cluster.reachable ||
    cluster.sync.state === "disconnected" ||
    cluster.sync.state === "stale"
  ) {
    return "degraded"
  }
  return "live"
}

export interface StageMeta {
  label: string
  tone: StatusTone
  /** One line under the header explaining what this stage means for the user. */
  blurb: string
}

export const CLUSTER_STAGE_META: Record<ClusterStage, StageMeta> = {
  onboarding: {
    label: "Not enrolled",
    tone: "info",
    blurb:
      "Declared in the plane, waiting for its operator. Install the cluster-setup chart to enrol it — observed state appears once it dials home.",
  },
  live: {
    label: "Live",
    tone: "ok",
    blurb: "Enrolled and reporting.",
  },
  degraded: {
    label: "Link degraded",
    tone: "warn",
    blurb:
      "The operator enrolled but its link is not healthy right now. The state below is the last the cluster reported, not live.",
  },
  draining: {
    label: "Draining",
    tone: "warn",
    blurb: "Being decommissioned. The plane is detaching from this cluster.",
  },
  archived: {
    label: "Archived",
    tone: "neutral",
    blurb: "Detached from the plane. Kept for its history; read-only.",
  },
}
