import { StatusDot } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import { useMemo } from "react"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import type { ArtifactFit, ServingFootprint } from "@/modules/demand/types"
import { usePlacementPreview } from "@/modules/placement/queries"
import type { ClusterVerdict } from "@/modules/placement/types"

/**
 * Whether the replica must land inside one fabric.
 *
 * A separate declaration from which cluster, and it belongs beside it because it is the
 * other half of the same question: the plane chooses the cluster and the constraint
 * travels with the choice, where the cluster's own scheduler honours it against its
 * topology domains. Rule 3, at the two ends it draws.
 *
 * The derived value is pre-selected and unticking it is allowed. That direction matters:
 * the shape implies the requirement, so the common case needs no thought, and the case
 * the derivation cannot see — a pipeline-parallel replica somebody still wants inside one
 * rack for latency — is a tick rather than an edit after the fact.
 *
 * An unchanged box sends the derived answer, because there is now one column and it is
 * not nullable: the workload's serving block carries the requirement outright, so what
 * is written is whatever the box reads.
 */
function FabricChoice({
  derived,
  value,
  onChange,
}: {
  derived: boolean
  value: boolean | null
  onChange: (fabric: boolean | null) => void
}) {
  const checked = value ?? derived
  return (
    <label className="flex cursor-pointer items-start gap-2.5 rounded-md border bg-muted/20 px-3 py-2">
      <Checkbox
        className="mt-0.5"
        checked={checked}
        onCheckedChange={(next) =>
          onChange(next === derived ? null : next === true)
        }
      />
      <div className="min-w-0">
        <span className="text-xs font-medium">
          Keep every replica inside one fabric
        </span>
        <p className="font-mono text-2xs text-muted-foreground">
          {derived
            ? "The shape needs it: a tensor group reaching over the backend fabric is the one split that does not work."
            : "Not needed by this shape — pipeline stages cross a network perfectly well. Tick it to ask for one anyway."}
          {value !== null && " Overriding what the shape derived."}
        </p>
      </div>
    </label>
  )
}

/**
 * Which clusters this may use — a default, an affinity and an escape hatch.
 *
 * The three modes are the k8s ones and they mean the same things. `auto` lets placement
 * decide, which is rule 3 and the only mode that should ever be common. `constrain`
 * narrows the set placement chooses from and still runs it, quota and topology and all
 * — a `nodeSelector`. `pin` skips placement entirely and is never checked for capacity,
 * which is `nodeName`, and it is labelled with that cost rather than presented as a
 * peer of the other two.
 *
 * Only the first is preselected. A cluster dropdown offered as an ordinary field is how
 * a scheduler stops being used.
 */
export type WhereMode = "auto" | "region" | "clusters" | "pin"

export interface WhereChoice {
  mode: WhereMode
  /** Regions allowed, for `region`. Any cluster inside one of them will do. */
  regions: string[]
  /** Clusters allowed, for `clusters`. Empty means the same as `auto`. */
  allowed: string[]
  /** The one cluster, for `pin`. */
  pinned: string | null
  /**
   * Whether every device of a replica must sit in one fabric.
   *
   * Null means "whatever the shape derived" — which is not the same as false, and is
   * why this is not a plain boolean. The shape implies the requirement in the common
   * case, so a value here is somebody disagreeing with what was derived.
   */
  fabric: boolean | null
}

export const AUTO_WHERE: WhereChoice = {
  mode: "auto",
  regions: [],
  allowed: [],
  pinned: null,
  fabric: null,
}

const MODES: { value: WhereMode; label: string; caption: string }[] = [
  {
    value: "auto",
    label: "Let placement decide",
    caption:
      "Quota, policy, topology and capacity, over every eligible cluster.",
  },
  {
    value: "region",
    label: "Any cluster in a region",
    caption:
      "Residency without micromanagement — the region is the constraint, the cluster is still placement's call.",
  },
  {
    value: "clusters",
    label: "Only these clusters",
    caption:
      "Placement still runs and still checks capacity — it just has fewer options.",
  },
  {
    value: "pin",
    label: "Pin to one",
    caption:
      "Placement is skipped and capacity is never checked. The 3am override.",
  },
]

export function WhereStep({
  value,
  onChange,
  footprint,
  project,
  replicas,
  fit,
  derivedFabric,
}: {
  value: WhereChoice
  onChange: (choice: WhereChoice) => void
  /**
   * The shape to ask the plane about, or null while nothing has been chosen.
   * Preview takes the shape itself, so the verdict arrives before the write.
   */
  footprint: ServingFootprint | null
  project: string | null
  replicas: number
  /** The chosen fitment row, which is all there is to go on before a shape is picked. */
  fit: ArtifactFit | null
  /**
   * What the chosen shape implies about fabric, which is what the checkbox starts on.
   * A tensor group crossing machines is a configuration that is always wrong, so a
   * tensor-parallel shape derives true and the box is ticked before anyone touches it.
   */
  derivedFabric: boolean
}) {
  const { data: clusters } = useClusterOptions()
  /**
   * No priority class. The flow does not ask for one, so preview must not assert one
   * either — the project's default is what a real create will be admitted against, and
   * a preview computed against a different class is a preview of a different question.
   */
  const preview = usePlacementPreview(
    {
      shape: footprint
        ? {
            device_class: footprint.device_class,
            device_count: footprint.device_count,
            tensor_parallel: footprint.tensor_parallel,
            requires_single_topology_domain: value.fabric ?? derivedFabric,
          }
        : null,
      project,
      replicas,
    },
    value.mode !== "pin"
  )

  /** The clusters a narrowing choice leaves in play, for filtering the verdict list. */
  const inPlay =
    value.mode === "region" && value.regions.length > 0
      ? (clusters ?? [])
          .filter((cluster) => value.regions.includes(cluster.region))
          .map((cluster) => cluster.id)
      : value.mode === "clusters"
        ? value.allowed
        : []

  /**
   * Regions the fleet actually has, not a fixed list. A region with no cluster in it is
   * not a choice, it is a typo waiting to happen.
   */
  const regions = useMemo(
    () =>
      [...new Set((clusters ?? []).map((cluster) => cluster.region))]
        .filter(Boolean)
        .sort(),
    [clusters]
  )

  return (
    <div className="space-y-3">
      {value.mode !== "pin" &&
        (footprint && project ? (
          <RealVerdict
            loading={preview.isLoading}
            summary={preview.data?.summary}
            placed={preview.data?.placed}
            cluster={preview.data?.cluster_name}
            verdicts={preview.data?.verdicts ?? []}
            allowed={inPlay}
          />
        ) : (
          <CapacityHint fit={fit} />
        ))}

      <FabricChoice
        derived={derivedFabric}
        value={value.fabric}
        onChange={(fabric) => onChange({ ...value, fabric })}
      />

      <div className="space-y-1.5">
        {MODES.map((mode) => (
          <label
            key={mode.value}
            className={cn(
              "flex cursor-pointer items-start gap-2.5 rounded-md border px-3 py-2 transition-colors",
              value.mode === mode.value
                ? "border-primary/40 bg-primary/5"
                : "hover:bg-muted/40"
            )}
          >
            <input
              type="radio"
              name="where-mode"
              className="mt-1 accent-primary"
              checked={value.mode === mode.value}
              onChange={() =>
                onChange({ ...value, mode: mode.value, pinned: null })
              }
            />
            <div className="min-w-0">
              <span className="text-xs font-medium">{mode.label}</span>
              <p className="font-mono text-2xs text-muted-foreground">
                {mode.caption}
              </p>
            </div>
          </label>
        ))}
      </div>

      {value.mode === "region" && (
        <div className="flex flex-wrap gap-2 rounded-md border bg-muted/20 p-3">
          {regions.map((region) => (
            <label
              key={region}
              className="flex cursor-pointer items-center gap-2 font-mono text-2xs"
            >
              <Checkbox
                checked={value.regions.includes(region)}
                onCheckedChange={(checked) =>
                  onChange({
                    ...value,
                    regions: checked
                      ? [...value.regions, region]
                      : value.regions.filter((entry) => entry !== region),
                  })
                }
              />
              {region}
              <span className="text-muted-foreground">
                {
                  (clusters ?? []).filter(
                    (cluster) => cluster.region === region
                  ).length
                }
              </span>
            </label>
          ))}
          {regions.length === 0 && (
            <p className="w-full font-mono text-2xs text-muted-foreground">
              No cluster reports a region yet.
            </p>
          )}
          {regions.length > 0 && value.regions.length === 0 && (
            <p className="w-full font-mono text-2xs text-muted-foreground">
              None selected, which is the same as letting placement decide.
            </p>
          )}
        </div>
      )}

      {value.mode === "clusters" && (
        <div className="flex flex-wrap gap-2 rounded-md border bg-muted/20 p-3">
          {(clusters ?? []).map((cluster) => (
            <label
              key={cluster.id}
              className="flex cursor-pointer items-center gap-2 font-mono text-2xs"
            >
              <Checkbox
                checked={value.allowed.includes(cluster.id)}
                onCheckedChange={(checked) =>
                  onChange({
                    ...value,
                    allowed: checked
                      ? [...value.allowed, cluster.id]
                      : value.allowed.filter((id) => id !== cluster.id),
                  })
                }
              />
              {clusterLabel(cluster)}
            </label>
          ))}
          {value.allowed.length === 0 && (
            <p className="w-full font-mono text-2xs text-muted-foreground">
              None selected, which is the same as letting placement decide.
            </p>
          )}
        </div>
      )}

      {value.mode === "pin" && (
        <div className="space-y-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-3">
          <p className="font-mono text-2xs text-warning">
            A pinned workload gets exactly one deployment on the cluster you
            name, whether or not it has the devices for it.
          </p>
          <div className="flex flex-wrap gap-2">
            {(clusters ?? []).map((cluster) => (
              <Button
                key={cluster.id}
                size="sm"
                variant={value.pinned === cluster.id ? "default" : "outline"}
                className="h-7 font-mono text-2xs"
                onClick={() => onChange({ ...value, pinned: cluster.id })}
              >
                {clusterLabel(cluster)}
              </Button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * The plane's own answer, for a configuration that does not exist yet.
 *
 * `verdicts` carry the losing clusters' reasoning too, and it is shown rather than
 * summarised: "6 free h100 in the largest nvlink domain, 8 asked for" is a next action
 * and "no capacity" is not.
 *
 * The decision comes back under the workload id `wkl_preview` and that id is
 * deliberately not rendered: nothing was created, and a link to it is a 404.
 */
function RealVerdict({
  loading,
  summary,
  placed,
  cluster,
  verdicts,
  allowed,
}: {
  loading: boolean
  summary?: string
  placed?: boolean
  cluster?: string | null
  verdicts: ClusterVerdict[]
  allowed: string[]
}) {
  if (loading) return <Skeleton className="h-16 w-full" />
  if (summary === undefined) return null

  const narrowed =
    allowed.length > 0
      ? verdicts.filter((verdict) => allowed.includes(verdict.cluster))
      : verdicts
  const winner = placed && cluster

  return (
    <div className="space-y-2 rounded-lg border bg-muted/20 p-3">
      <div className="flex items-center gap-2">
        <StatusDot tone={winner ? "ok" : "warn"} />
        <span className="font-mono text-xs">
          {winner ? cluster : "Nowhere it fits right now"}
        </span>
        <Badge variant="outline" className="font-mono text-2xs">
          preview
        </Badge>
      </div>
      <p className="font-mono text-2xs text-muted-foreground">{summary}</p>
      {narrowed.length > 0 && (
        <div className="space-y-1 border-t pt-2">
          {narrowed.map((verdict) => (
            <div
              key={verdict.cluster}
              className="flex items-baseline justify-between gap-3 font-mono text-2xs"
            >
              <span className="flex items-center gap-1.5">
                <StatusDot tone={verdict.feasible ? "ok" : "neutral"} />
                <span
                  className={
                    verdict.feasible ? "" : "text-muted-foreground line-through"
                  }
                >
                  {verdict.cluster_name}
                </span>
              </span>
              <span className="min-w-0 truncate text-right text-muted-foreground">
                {verdict.feasible
                  ? `${verdict.free_devices} free · slack ${verdict.fit_slack}`
                  : (verdict.reasons[0]?.message ?? "ruled out")}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * What can be said before a shape or a project has been chosen, and no more.
 *
 * Not a verdict and it does not pretend to be: the fit knows how many devices of the
 * class are allocatable across how many clusters, and it explicitly does not know
 * whether `device_count` of them share a topology domain. Calling this a placement
 * decision would be the one lie the fitment panel already goes out of its way not to
 * tell.
 */
function CapacityHint({ fit }: { fit: ArtifactFit | null }) {
  if (!fit)
    return (
      <p className="rounded-lg border bg-muted/20 p-3 font-mono text-2xs text-muted-foreground">
        Placement picks the cluster when you deploy. The workload page shows
        what it decided, and why every other cluster lost.
      </p>
    )
  const short = fit.devices_allocatable < fit.device_count
  return (
    <div className="space-y-1.5 rounded-lg border bg-muted/20 p-3">
      <div className="flex items-center gap-2">
        <StatusDot tone={short ? "warn" : "info"} />
        <span className="font-mono text-xs">
          {fit.devices_allocatable} of {fit.devices_total} {fit.label} free
          across {fit.cluster_count} cluster
          {fit.cluster_count === 1 ? "" : "s"}
        </span>
        <Tooltip>
          <TooltipTrigger asChild>
            <Badge variant="outline" className="font-mono text-2xs">
              not a verdict
            </Badge>
          </TooltipTrigger>
          <TooltipContent className="max-w-80">
            Free devices say nothing about whether {fit.device_count} of them
            share a topology domain — placement answers that when you deploy,
            and the workload page shows what it decided.
          </TooltipContent>
        </Tooltip>
      </div>
      <p className="font-mono text-2xs text-muted-foreground">
        {short
          ? `Fewer free than the ${fit.device_count} a replica needs. Placement will queue it rather than reject it.`
          : "Placement picks the cluster when you deploy."}
      </p>
    </div>
  )
}
