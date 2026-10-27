/**
 * `ClusterSchema.phase`: declared. What we asked for, and never where
 * reachability lives: folding "the network is down" into the same field as
 * "somebody disabled this cluster" makes two situations that need different
 * responses indistinguishable.
 */
export type ClusterPhase = "pending" | "active" | "draining" | "disabled"

/**
 * `ClusterSyncSchema.state`: observed and derived, never stored. Left open
 * because the plane derives this value and may derive one the console has not
 * been taught about yet.
 */
export type SyncState =
  | "connected"
  | "disconnected"
  | "never_connected"
  /** Link up, cluster silent. Reachable says nothing about whether it still reports. */
  | "stale"
  | (string & {})

/** Observed liveness of a cluster's agent link. */
export interface ClusterSync {
  state: SyncState
  last_sync_at: string | null
  last_seen_at: string | null
  operator_version: string | null
  lag_ms: number | null
  last_error: string | null
  /** What the cluster turned out to be, as opposed to how it was enrolled. */
  distribution: string | null
  cloud: string | null
  observed_region: string | null
  /**
   * The operator runs without its reconcile loop: it reports everything and
   * applies nothing the plane declares. Orthogonal to `state`: a read-only
   * cluster is `connected` and healthy by every other field here, which is why
   * it has to be shown on its own. Anything placed on it never starts.
   */
  read_only: boolean
  /**
   * A leader-worker backend, Grove or LeaderWorkerSet, is installed, so this
   * cluster can run a replica whose devices do not fit on one machine. Observed
   * from the API groups the cluster serves, not configured, so installing
   * a backend is enough to make it true and nothing has to be redeclared.
   */
  multinode: boolean
}

/**
 * Where the data plane reaches this cluster's serving runtime. Declared, and
 * not part of the cluster patch body: the plane keeps it as its own
 * sub-resource because nothing in the control plane ever dials it, only the
 * gateway does.
 */
export interface ClusterServing {
  /** Opaque to hangar: a public ingress, an in-cluster service name, anything dialable. */
  url: string
  /** A reference to whatever fronts the address, never the secret itself. */
  credential_id: string | null
  enabled: boolean
}

export interface ClusterServingRequest {
  url: string
  credential_id?: string | null
  enabled?: boolean
}

export interface Cluster {
  id: string
  name: string
  display_name: string | null
  region: string
  zone: string | null
  residency: string | null
  phase: ClusterPhase
  labels: Record<string, string>
  capabilities: Record<string, unknown>
  enrolled_at: string | null
  created_at: string
  updated_at: string | null
  revision: string
  /** Observed. A cluster can be `active` and unreachable at the same time. */
  reachable: boolean
  sync: ClusterSync
  /** Null until an address is declared. A cluster is schedulable without one. */
  serving: ClusterServing | null
  /**
   * The `cluster` label this cluster's series are stamped with in the shared
   * metrics store. Defaults to the cluster's own name at registration, since
   * that is what the setup chart stamps. Null means it is not observable and its
   * panels say so, instead of querying under a label nobody has confirmed.
   */
  metrics_label: string | null
}

/**
 * Everything the register-cluster screen shows to make a cluster hangar-ready:
 * a `helm` install of the `cluster-setup` chart. `values_yaml` is one editable
 * file: plane-filled identity up top, a commented skeleton of the cluster's own
 * infra facts below to fill in. The enrollment token is shown once when minted
 * (`enrolment_token`, folded into `values_yaml`); a plain GET carries none and
 * `enrolment_token_set` says whether one already exists.
 */
export interface ClusterSetup {
  helm_command: string
  values_yaml: string
  /** `values.<cluster>.local.yaml`: what to save `values_yaml` as; the helm command reads this name. */
  values_filename: string
  chart_ref: string
  chart_version: string | null
  registry_login: string | null
  /** The same login via an existing `gh` session; null for a non-GitHub registry. */
  registry_login_gh: string | null
  /** Present only in the mint/rotate response: shown once, never returned by a GET. */
  enrolment_token: string | null
  /** Whether a token already exists, so the screen offers "rotate" instead of "generate". */
  enrolment_token_set: boolean
}

/**
 * How much of a cluster is free to place onto, derived at read time from
 * observed devices: nothing here is stored. `largest_contiguous_block` is the
 * biggest set of allocatable devices sharing one topology domain (what a job
 * wanting one fabric can actually get), distinct from `free_devices` which
 * counts every allocatable device regardless of where it sits.
 * `whole_nodes_free` counts nodes on which every device is allocatable.
 */
export interface ClusterHeadroom {
  free_devices: number
  whole_nodes_free: number
  largest_contiguous_block: number
}

/**
 * A cluster's observed footprint in aggregate, for the list and card views.
 * Every field is derived at read time. `devices_allocated` is
 * `devices_total - devices_allocatable`; `pool_count` and `deployment_count`
 * are the declared objects hanging off the cluster, the rest are observed.
 */
export interface ClusterRollup {
  cluster_id: string
  nodes_total: number
  nodes_ready: number
  devices_total: number
  devices_allocatable: number
  devices_allocated: number
  pool_count: number
  deployment_count: number
  headroom: ClusterHeadroom
}

export interface ClusterCreateRequest {
  name: string
  region: string
  display_name?: string | null
  zone?: string | null
  residency?: string | null
  phase?: ClusterPhase
  labels?: Record<string, string>
  capabilities?: Record<string, unknown>
  /** Omitted defaults to the cluster's name; null registers it as not observable. */
  metrics_label?: string | null
}

export interface ClusterUpdateRequest {
  display_name?: string | null
  region?: string
  zone?: string | null
  residency?: string | null
  phase?: ClusterPhase
  labels?: Record<string, string>
  capabilities?: Record<string, unknown>
  /** Null stops this cluster being queried for metrics at all. */
  metrics_label?: string | null
}
