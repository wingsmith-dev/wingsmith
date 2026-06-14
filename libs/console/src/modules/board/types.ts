import type { Cluster } from "@/modules/clusters/types"
import type { WorkloadStrategy } from "@/modules/demand/types"
import type { Reservation } from "@/modules/placement/types"
import type { Device, TopologyDomain } from "@/modules/supply/types"

/**
 * Where a holder came from, and how much to believe about it.
 *
 * `live` is an allocation the cluster reports — observed, and true of exactly
 * these devices. `moved` and `proposed` exist only in the sandbox and are never
 * sent anywhere. `reserved` is the odd one and the only one that is neither: the
 * plane has declared that this cluster owes these devices to a deployment, which
 * is as real as an allocation, but the cluster has not yet said *which*
 * accelerators it will bind. The count is exact; the cells wearing it are a
 * plausible guess the board made so that the promise has somewhere to be seen.
 */
export type HolderOrigin = "live" | "moved" | "proposed" | "reserved"

/**
 * What holds a device, flattened out of the allocation that holds it.
 *
 * Both halves of the board render from this one shape, which is the point: a
 * proposed placement paints exactly the way the live one does, and the only
 * difference a reader has to hold on to is `origin`.
 */
export interface Holder {
  allocation: string
  workload: string
  workload_name: string
  project_slug: string
  device_class: string
  device_count: number
  phase: string
  gang_complete: boolean
  origin: HolderOrigin
}

/** Device id → what holds it. The single input the board is painted from. */
export type Assignments = Map<string, Holder>

/**
 * Device state as one cell of the board.
 *
 * `slices` is the intra-device layer and is the MIG children of this device —
 * separate `Device` rows in the plane's model, tied here to the parent they name
 * in `mig_parent_device_uuid`. A partitioned device holds nothing itself; its
 * slices do.
 */
export interface BoardDevice {
  device: Device
  holder: Holder | null
  /** Its node is cordoned. Not a device fact, so it is carried beside one. */
  cordoned: boolean
  slices: BoardDevice[]
}

/**
 * The tally, at every layer of the board.
 *
 * The first five are device facts and are counted off the cells. A device the
 * board has painted for a reservation still counts as `free` and never as
 * `held`, which is the distinction the whole feature turns on: nothing is
 * holding it, something is owed it.
 *
 * `reserved` means two subtly different things by layer, and the difference is
 * deliberate. On a fabric it is the cells the board painted there — indicative,
 * because the cluster has not chosen. On a cluster it is replaced by the exact
 * total the plane says is owed, which is the number to trust and the one that
 * can exceed what the cluster has free. A cluster owing more than it holds
 * therefore reports more `reserved` than its fabrics do, and that gap is real.
 *
 * `available` is `free` less `reserved`, floored at zero, and it exists because
 * subtracting the two afterwards gives the wrong number the moment a tally is
 * summed: a region's spare capacity is the sum of what each of its clusters can
 * still offer, and one cluster's idle devices cannot pay another's promise.
 */
export interface BoardCounts {
  total: number
  held: number
  free: number
  fault: number
  cordoned: number
  reserved: number
  available: number
}

/**
 * One topology domain and the devices in it — the fabric layer.
 *
 * `domain` is null for devices whose cluster reports no topology at all. They
 * are still schedulable, so they get a fabric of their own rather than being
 * dropped from the board; nothing that needs one fabric can land there.
 */
export interface BoardFabric {
  key: string
  /** The cluster's own name for the domain. Sometimes that is just its uid. */
  identifier: string
  /**
   * What to put in front of a person. The identifier when it says something, and
   * the nodes the fabric covers when it does not — a cluster that reports the
   * domain's uid as its identifier has told us nothing, and thirty rows of
   * `tpd_01j…` is a board nobody can read.
   */
  label: string
  kind: string
  domain: TopologyDomain | null
  cluster: string
  cluster_name: string
  nodes: string[]
  node_names: string[]
  device_class: string | null
  /** Every class present, sorted. One entry is the ordinary, homogeneous case. */
  device_classes: string[]
  devices: BoardDevice[]
  counts: BoardCounts
  /** Ids of devices nothing holds, allocatable, in device-index order. */
  free: string[]
}

export interface BoardCluster {
  cluster: Cluster
  fabrics: BoardFabric[]
  counts: BoardCounts
  /**
   * The biggest set of devices sharing one fabric that nothing holds and nothing
   * is owed — the largest gang this cluster could still take.
   *
   * Net of reservations, because the board has painted them onto cells and a
   * number that contradicted the picture beside it would be worse than either
   * answer alone. It is therefore as indicative as the painting is.
   */
  largest_block: number
  /**
   * What is on its way here, per deployment.
   *
   * The exact list, and the authority for every reserved number on the screen.
   * The cells painted from it are chosen here and are indicative — the cluster
   * has not said which accelerators it will bind — so where the two disagree,
   * this is the one that is true and the painting is the one that is a guess.
   */
  reservations: Reservation[]
}

export interface BoardRegion {
  region: string
  clusters: BoardCluster[]
  counts: BoardCounts
}

export interface Board {
  regions: BoardRegion[]
  counts: BoardCounts
  /** Every device on the board by id, physical and slice alike. */
  devices: Map<string, BoardDevice>
  fabrics: Map<string, BoardFabric>
  /** Device id → the fabric key it sits in. */
  fabric_of: Map<string, string>
}

/**
 * A whole workload to place — the brick. Either a real one, read off its serving
 * block and its scale, or one somebody typed into the shape builder.
 *
 * It names no cluster and no fabric: what a brick is and where it could go are
 * different questions, and the board answers the second one.
 *
 * `device_count` is one replica's shape and `replicas` is how many of them are
 * wanted. Those are deliberately separate numbers rather than a total, because
 * every rule that decides where a workload lands applies per replica — a gang
 * either fits inside one fabric or it does not, and forty-eight devices spread
 * across six fabrics is a satisfied workload while one forty-eight-device
 * requirement is an unschedulable one.
 */
export interface Brick {
  key: string
  label: string
  detail: string
  /** Null for a hand-built shape that belongs to no workload. */
  workload: string | null
  /**
   * The project to ask on behalf of, when there is a workload to ask about.
   *
   * With `workload` set it makes the brick a question the plane can answer —
   * quota and policy are per project. Absent, the brick is a hypothesis and only
   * the board's own arithmetic applies to it.
   */
  project: string | null
  priority_class: string | null
  project_slug: string
  device_class: string
  /** Devices per replica. */
  device_count: number
  requires_single_topology_domain: boolean
  topology_domain_kind: string | null
  /** Replicas wanted in total, across every cluster. `Workload.scale.desired`. */
  replicas: number
  /** `pack` fills one cluster before reaching for the next; `spread` levels them. */
  strategy: WorkloadStrategy
  /** `{cluster_uid: minimum replicas}`. An unknown uid is ignored, never an error. */
  cluster_floors: Record<string, number>
  /** How many clusters this workload may be spread over at once. */
  max_clusters: number | null
  /**
   * The workload names one cluster and only that cluster. A floor is a minimum
   * and the rest may land anywhere; a pin is not — it is the deliberate
   * scheduler escape, and a pinned replica that cannot fit where it was pinned
   * does not fit at all.
   */
  pinned: boolean
}

/**
 * Where one replica of a brick would land.
 *
 * A failed replica is a row like any other and carries the reason it failed —
 * "four of six placed" is the answer placement actually gives, and reporting it
 * as a bare failure would throw away the four that fit.
 */
export interface ReplicaPlan {
  index: number
  ok: boolean
  cluster: string | null
  cluster_name: string | null
  fabric: string | null
  fabric_identifier: string | null
  devices: string[]
  reason: string | null
}

/** A whole workload's worth of replicas, planned against one board. */
export interface PlacementPlan {
  brick: Brick
  /** Replicas this pass tried to place — desired, less those already held. */
  wanted: number
  replicas: ReplicaPlan[]
  placed: number
  /** Cluster ids the plan would use, in the order it reached for them. */
  clusters: string[]
  /** Floors the fleet could not honour, as readable lines. */
  unmet: string[]
}

/**
 * One proposed change. Nothing here is a write — the sandbox is a client-side
 * overlay, and the plane has no endpoint that takes any of it.
 */
export type Move =
  | {
      id: string
      kind: "place"
      brick: Brick
      devices: string[]
      /** Which replica of the brick this is, when a whole workload was planned. */
      replica?: number
      /**
       * The planning pass that produced it. Placing a workload's replicas is one
       * decision, so undo takes the whole batch back out rather than unpicking it
       * one replica at a time.
       */
      batch?: string
    }
  | {
      id: string
      kind: "move"
      allocation: string
      holder: Holder
      devices: string[]
      from: string[]
    }
  | {
      id: string
      kind: "release"
      allocation: string
      holder: Holder
      from: string[]
    }

/** Whether a brick fits somewhere, and which devices it would take if it does. */
export interface Fit {
  ok: boolean
  devices: string[]
  reason: string | null
}

/**
 * What a plan did to the fleet's shape. Counted over the whole board rather
 * than the clusters a plan touched, because packing one cluster tighter at the
 * cost of another is the thing worth seeing.
 */
export interface BoardMetrics {
  total: number
  held: number
  free: number
  /** The biggest free set inside a single fabric, fleet-wide. */
  largest_block: number
  /** Fabrics where nothing is held — what a whole-fabric job can still have. */
  whole_fabrics_free: number
  /** Fabrics that are partly held and partly free. Fragmentation, counted. */
  fabrics_split: number
}

/** What the inspector is pointed at. */
export type Selection =
  | { kind: "device"; id: string }
  | { kind: "fabric"; key: string }
  | { kind: "cluster"; id: string }
  | { kind: "region"; region: string }
  | null
