import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { ACTIVATION_FOCUS, activationProps } from "@/lib/keyboard"
import { cn } from "@/lib/utils"
import { assignmentsOf } from "@/modules/board/build"
import { brickColor } from "@/modules/board/palette"
import { useBricks } from "@/modules/board/queries"
import {
  planFromShares,
  planPlacement,
  replicasPlaced,
} from "@/modules/board/sandbox"
import { useBoardStore } from "@/modules/board/store"
import type {
  Assignments,
  Board,
  Brick,
  PlacementPlan,
} from "@/modules/board/types"
import {
  useArtifactFitment,
  useArtifactOptions,
} from "@/modules/demand/queries"
import type { ArtifactFit } from "@/modules/demand/types"
import { useProjectOptions } from "@/modules/binding/queries"
import {
  usePlacementPreview,
  useWorkloadExpansion,
} from "@/modules/placement/queries"
import { useDeviceClasses } from "@/modules/supply/queries"
import {
  IconBolt,
  IconChevronRight,
  IconGripVertical,
  IconPlus,
  IconX,
} from "@tabler/icons-react"
import { useMemo, useState } from "react"
import { toast } from "sonner"
import { Verdicts } from "./Verdicts"

/**
 * The bricks: what could run, in the two forms the plane can describe it.
 *
 * A **workload** is a commitment: it has a scale, spread rules and replicas
 * already holding devices, and it carries its own serving block, so the shape is
 * read straight off it. An **artifact** sits one step further back and answers a
 * different question (fitment says which cards could hold these weights at all),
 * so its rows arrive here as candidate shapes, not as placements.
 *
 * Where the answer comes from matters and is labeled. A brick the plane can be
 * asked about (a declared workload, or any shape once somebody says which project
 * to charge) gets an authoritative verdict: only the plane sees quota, priority,
 * policy, residency and what would have to be preempted. Everything else is the
 * board's own arithmetic over devices and fabrics, marked `est.` because it can be
 * right about hardware and wrong about admission. Where the two disagree the plane
 * is the answer and the board was always a guess.
 */
export function Tray({ board }: { board: Board | undefined }) {
  const { bricks, isLoading } = useBricks()
  const armed = useBoardStore((s) => s.armed)
  const holding = useBoardStore((s) => s.holding)
  const arm = useBoardStore((s) => s.arm)
  const custom = useBoardStore((s) => s.custom)

  /**
   * Who an undeclared shape would be charged to.
   *
   * A workload names its own project; a shape nobody has declared does not, and
   * the plane will not answer without one because quota and policy are per
   * project. So it is asked once here, not per row.
   */
  const { data: projects } = useProjectOptions()
  const [project, setProject] = useState<string | null>(null)
  const asProject = project ?? projects?.[0]?.slug ?? null

  const assignments = useMemo(
    () => (board ? assignmentsOf(board) : new Map()),
    [board]
  )

  const rowProps = { board, assignments }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center justify-between text-sm">
          Bricks
          {armed && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => arm(null)}
              className="h-5 gap-1 px-1.5 font-mono text-2xs text-muted-foreground"
            >
              <IconX className="size-3" />
              cancel
            </Button>
          )}
        </CardTitle>
        <p className="font-mono text-2xs text-muted-foreground">
          {armed && holding
            ? `${armed.label} in hand · click where it should go, or click it again to put it back`
            : armed
              ? `${armed.label} in hand · click a device or fabric to place one replica`
              : "click or drag to place one replica · the bolt plans the rest"}
        </p>
      </CardHeader>

      <CardContent className="space-y-3 px-2">
        {isLoading && (
          <p className="px-2 font-mono text-2xs text-muted-foreground">
            Loading…
          </p>
        )}

        {!isLoading && (
          <Group
            label="Workloads"
            empty="Nothing is declared yet. Size an artifact below to see where it would go."
            count={bricks.length}
          >
            {bricks.map((brick) => (
              <BrickRow key={brick.key} brick={brick} {...rowProps} />
            ))}
          </Group>
        )}

        <div className="flex items-center gap-2 px-1.5 pt-1">
          <Label className="font-mono text-2xs text-muted-foreground">
            as project
          </Label>
          <Select
            value={asProject ?? ""}
            onValueChange={setProject}
            disabled={!projects?.length}
          >
            <SelectTrigger size="sm" className="h-6 flex-1 font-mono text-2xs">
              <SelectValue placeholder="pick one" />
            </SelectTrigger>
            <SelectContent>
              {(projects ?? []).map((entry) => (
                <SelectItem
                  key={entry.id}
                  value={entry.slug}
                  className="font-mono text-xs"
                >
                  {entry.slug}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <Group label="Artifacts" empty="" count={1}>
          <ArtifactShapes project={asProject} {...rowProps} />
        </Group>

        <Group label="Hand-built" empty="" count={1}>
          {custom && (
            <BrickRow brick={{ ...custom, project: asProject }} {...rowProps} />
          )}
          <ShapeBuilder />
        </Group>
      </CardContent>
    </Card>
  )
}

function Group({
  label,
  empty,
  count,
  children,
}: React.PropsWithChildren<{ label: string; empty: string; count: number }>) {
  return (
    <div className="space-y-1">
      <div className="px-1.5 font-mono text-2xs tracking-[0.15em] text-muted-foreground uppercase">
        {label}
      </div>
      {count === 0 && empty ? (
        <p className="px-1.5 font-mono text-2xs leading-relaxed text-muted-foreground">
          {empty}
        </p>
      ) : null}
      {children}
    </div>
  )
}

/**
 * One brick, and where it would go.
 *
 * Collapsed, it shows the board's own count: replicas held over replicas wanted,
 * and how many more the devices could take. Expanding asks the plane, and its
 * answer replaces the estimate: lazily, because each call costs the plane a pass
 * over quota, policy and every cluster's capacity, and a tray of thirty bricks
 * should not ask thirty times to fill a column nobody has looked at.
 */
function BrickRow({
  brick,
  board,
  assignments,
  className,
}: {
  brick: Brick
  board: Board | undefined
  assignments: Assignments
  className?: string
}) {
  const armed = useBoardStore((s) => s.armed)
  const arm = useBoardStore((s) => s.arm)
  const drag = useBoardStore((s) => s.drag)
  const placeMany = useBoardStore((s) => s.placeMany)

  const [open, setOpen] = useState(false)
  const color = brickColor(brick.workload ?? brick.key)
  /**
   * A declared workload is asked about by name; anything else is asked about by
   * shape, which `POST /placement/preview` now takes. Either way it needs a
   * project, because capacity is read per project: a shape with nobody to charge
   * it to is the one case that still falls back to the board's own arithmetic.
   */
  const askable = !!brick.workload || !!brick.project

  const expansion = useWorkloadExpansion(
    brick.workload,
    open && askable && !!brick.workload
  )
  const preview = usePlacementPreview(
    {
      shape: {
        device_class: brick.device_class,
        device_count: brick.device_count,
        requires_single_topology_domain: brick.requires_single_topology_domain,
        topology_domain_kind: brick.topology_domain_kind,
      },
      project: brick.project,
      priority_class: brick.priority_class,
      replicas: brick.replicas,
    },
    open && askable && !brick.workload
  )
  const asked = brick.workload ? expansion : preview

  /** The board's own answer: what the devices could take, ignoring admission. */
  const estimate = useMemo(() => {
    if (!board) return null
    const held = replicasPlaced(board, brick.workload)
    const wanted = Math.max(brick.replicas - held, 0)
    return {
      held,
      wanted,
      plan: planPlacement(board, brick, assignments, wanted),
    }
  }, [board, brick, assignments])

  /**
   * The plane's decision, turned into devices. Its shares say which clusters and
   * how many each; choosing devices inside them stays local, which is the split
   * the object model draws.
   */
  const directed: PlacementPlan | null = useMemo(() => {
    if (!board) return null
    if (expansion.data) {
      return planFromShares(board, brick, assignments, expansion.data.shares)
    }
    if (preview.data?.placed && preview.data.cluster) {
      return planFromShares(board, brick, assignments, [
        { cluster: preview.data.cluster, replicas: brick.replicas },
      ])
    }
    return null
  }, [board, brick, assignments, expansion.data, preview.data])

  const plan = directed ?? estimate?.plan ?? null
  const authoritative = !!directed

  const planAll = (event: React.MouseEvent) => {
    event.stopPropagation()
    if (!plan) return
    const ok = plan.replicas.filter((replica) => replica.ok)
    if (ok.length > 0) {
      placeMany(
        ok.map((replica) => ({
          brick,
          devices: replica.devices,
          replica: replica.index,
        }))
      )
    }
    const lines = [
      ...new Set(
        plan.replicas
          .filter((replica) => !replica.ok)
          .map((replica) => replica.reason ?? "no room")
      ),
      ...plan.unmet,
    ]
    const source = authoritative ? "the plane's split" : "board estimate"
    const summary = `${ok.length} of ${plan.wanted} replicas · ${source}`
    if (lines.length > 0) {
      toast.warning(`${brick.label}: ${summary}`, {
        description: lines.slice(0, 3).join("\n"),
      })
    } else {
      toast.success(`${brick.label} planned`, { description: summary })
    }
  }

  return (
    <div className={cn("min-w-0", className)}>
      <div
        draggable
        onDragStart={(event) => {
          event.dataTransfer.effectAllowed = "copy"
          event.dataTransfer.setData("text/plain", brick.key)
          drag({ kind: "brick", brick })
        }}
        {...activationProps(() => arm(armed?.key === brick.key ? null : brick))}
        role="button"
        aria-pressed={armed?.key === brick.key}
        className={cn(
          "flex min-w-0 cursor-grab items-center gap-2 rounded-md px-1.5 py-1.5 transition-colors hover:bg-accent/50 active:cursor-grabbing",
          ACTIVATION_FOCUS,
          armed?.key === brick.key &&
            "bg-primary/10 ring-1 ring-primary/40 ring-inset"
        )}
      >
        <IconGripVertical className="size-3 shrink-0 text-muted-foreground" />
        <span className={cn("size-2.5 shrink-0 rounded-[2px]", color.swatch)} />
        <span className="min-w-0 flex-1">
          <span className="block truncate font-mono text-2xs">
            {brick.label}
          </span>
          <span className="block truncate font-mono text-2xs text-muted-foreground">
            {brick.detail}
          </span>
        </span>

        {plan && estimate && (
          <span className="shrink-0 text-right font-mono text-2xs tabular-nums">
            {brick.workload && (
              <span className="block">
                {estimate.held}/{brick.replicas}
              </span>
            )}
            <span
              className={cn(
                "block",
                plan.wanted === 0
                  ? "text-muted-foreground"
                  : plan.placed === plan.wanted
                    ? "text-success"
                    : plan.placed === 0
                      ? "text-destructive"
                      : "text-warning"
              )}
              title={
                authoritative
                  ? "from the plane's decision"
                  : "the board's own arithmetic: ignores quota, policy and preemption"
              }
            >
              {plan.wanted === 0
                ? "placed"
                : `+${plan.placed}${authoritative ? "" : " est."}`}
            </span>
          </span>
        )}

        {plan && plan.wanted > 0 && (
          <Button
            variant="ghost"
            size="sm"
            onClick={planAll}
            disabled={plan.placed === 0}
            aria-label={`Plan ${plan.wanted} replicas of ${brick.label}`}
            title={`Plan ${plan.wanted} outstanding ${
              plan.wanted === 1 ? "replica" : "replicas"
            }`}
            className="size-6 shrink-0 p-0 text-muted-foreground hover:text-primary"
          >
            <IconBolt className="size-3.5" />
          </Button>
        )}

        {askable && (
          <Button
            variant="ghost"
            size="sm"
            onClick={(event) => {
              event.stopPropagation()
              setOpen(!open)
            }}
            aria-label={open ? "Hide the plane's answer" : "Ask the plane"}
            title="ask the plane"
            className="size-6 shrink-0 p-0 text-muted-foreground hover:text-primary"
          >
            <IconChevronRight
              className={cn(
                "size-3.5 transition-transform",
                open && "rotate-90"
              )}
            />
          </Button>
        )}
      </div>

      {open && askable && (
        <Verdicts
          verdicts={asked.data?.verdicts}
          shares={expansion.data?.shares}
          shortfall={expansion.data?.shortfall}
          summary={asked.data?.summary}
          error={asked.error}
          isLoading={asked.isLoading}
          disagreement={
            estimate && asked.data && !directed && estimate.plan.placed > 0
              ? `The board finds room for ${estimate.plan.placed} of ${estimate.plan.wanted} on devices alone. The decision above is the authority: it also weighs quota, policy and preemption, and asks for a replica's devices contiguously.`
              : undefined
          }
        />
      )}
    </div>
  )
}

/**
 * An artifact's fitment, as bricks.
 *
 * This is the sizing question, not the placement one: which cards could hold
 * these weights at the window they publish, and how many of them. The plane says
 * so itself: a fit is not a promise a replica can be placed, because contiguity
 * inside a topology domain decides that. So these rows are shapes to try on the
 * board and, once a project is named, ones the plane will rule on directly.
 */
function ArtifactShapes({
  project,
  board,
  assignments,
}: {
  project: string | null
  board: Board | undefined
  assignments: Assignments
}) {
  const { data: artifacts } = useArtifactOptions()
  const [artifact, setArtifact] = useState("")
  const fitment = useArtifactFitment(artifact)

  const bricks = useMemo(() => {
    const items = fitment.data?.items ?? []
    return items
      .filter((fit) => fit.fits)
      .map((fit) => brickOfFit(fit, artifact))
  }, [fitment.data, artifact])

  return (
    <div className="space-y-1">
      <Select value={artifact} onValueChange={setArtifact}>
        <SelectTrigger size="sm" className="h-6 w-full font-mono text-2xs">
          <SelectValue placeholder="size an artifact" />
        </SelectTrigger>
        <SelectContent>
          {(artifacts ?? []).map((entry) => (
            <SelectItem
              key={entry.id}
              value={entry.id}
              className="font-mono text-xs"
            >
              {entry.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      {artifact && fitment.isLoading && (
        <p className="px-1.5 font-mono text-2xs text-muted-foreground">
          sizing…
        </p>
      )}
      {fitment.data?.note && (
        <p className="px-1.5 font-mono text-2xs leading-relaxed text-muted-foreground">
          {fitment.data.note}
        </p>
      )}
      {artifact && !fitment.isLoading && bricks.length === 0 && (
        <p className="px-1.5 font-mono text-2xs leading-relaxed text-muted-foreground">
          Nothing in the catalog holds these weights at{" "}
          {fitment.data?.context_length ?? "?"} tokens.
        </p>
      )}
      {bricks.map((brick) => (
        <BrickRow
          key={brick.key}
          brick={{ ...brick, project }}
          board={board}
          assignments={assignments}
        />
      ))}
    </div>
  )
}

function brickOfFit(fit: ArtifactFit, artifact: string): Brick {
  return {
    key: `fit:${artifact}:${fit.device_class}`,
    label: `${fit.device_count}× ${fit.label}`,
    detail: [
      `${fit.required_gb.toFixed(0)} GB needed`,
      fit.node_count > 1
        ? `tp${fit.tensor_parallel} · pp${fit.pipeline_parallel} over ${fit.node_count} nodes`
        : `tp${fit.tensor_parallel} · one node`,
      fit.kv_counted
        ? `${fit.context_length} ctx`
        : "weights only · window uncosted",
    ].join(" · "),
    workload: null,
    project: null,
    priority_class: null,
    project_slug: "sizing",
    device_class: fit.device_class,
    device_count: fit.device_count,
    /**
     * The fit says how the replica is laid out, so this is read instead of
     * guessed: tensor-parallel ranks share a node's interconnect, and pipeline
     * stages cross the backend fabric between nodes. A replica spanning nodes is
     * a normal answer for a large model, not a degraded one, so `node_count`
     * above 1 is what says it may spread.
     */
    requires_single_topology_domain: fit.node_count <= 1,
    topology_domain_kind: null,
    replicas: 1,
    strategy: "pack",
    cluster_floors: {},
    max_clusters: null,
    pinned: false,
  }
}

/**
 * A shape nobody has declared yet. The point of the sandbox is asking whether
 * eight of something would fit before there is a workload to ask on behalf of.
 */
function ShapeBuilder() {
  const { data: classes } = useDeviceClasses({ present: true })
  const setCustom = useBoardStore((s) => s.setCustom)
  const arm = useBoardStore((s) => s.arm)

  const [open, setOpen] = useState(false)
  const [deviceClass, setDeviceClass] = useState("")
  const [count, setCount] = useState(8)
  const [replicas, setReplicas] = useState(1)
  const [single, setSingle] = useState(true)
  const [spread, setSpread] = useState(false)

  const chosen = deviceClass || classes?.[0]?.name || ""

  if (!open) {
    return (
      <Button
        variant="ghost"
        size="sm"
        onClick={() => setOpen(true)}
        className="w-full justify-start gap-1.5 px-1.5 font-mono text-2xs text-muted-foreground"
      >
        <IconPlus className="size-3" />
        custom shape
      </Button>
    )
  }

  const build = () => {
    const wanted = Math.max(1, replicas)
    const brick: Brick = {
      key: `shape:${chosen}:${count}:${wanted}:${single}:${spread}`,
      label: `${wanted} × ${count}× ${chosen}`,
      detail: [
        single ? "one fabric" : "may spread",
        spread ? "spread" : "pack",
        "hand-built",
      ].join(" · "),
      workload: null,
      project: null,
      priority_class: null,
      project_slug: "sandbox",
      device_class: chosen,
      device_count: Math.max(1, count),
      requires_single_topology_domain: single,
      topology_domain_kind: null,
      replicas: wanted,
      strategy: spread ? "spread" : "pack",
      cluster_floors: {},
      max_clusters: null,
      pinned: false,
    }
    setCustom(brick)
    arm(brick)
    setOpen(false)
  }

  return (
    <div className="space-y-2 rounded-md border border-dashed p-2">
      <Select value={chosen} onValueChange={setDeviceClass}>
        <SelectTrigger size="sm" className="w-full font-mono text-2xs">
          <SelectValue placeholder="device class" />
        </SelectTrigger>
        <SelectContent>
          {(classes ?? []).map((entry) => (
            <SelectItem
              key={entry.name}
              value={entry.name}
              className="font-mono text-xs"
            >
              {entry.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <div className="flex items-center gap-2">
        <Label
          htmlFor="shape-count"
          className="flex-1 font-mono text-2xs text-muted-foreground"
        >
          devices / replica
        </Label>
        <Input
          id="shape-count"
          type="number"
          min={1}
          value={count}
          onChange={(event) => setCount(Number(event.target.value))}
          className="h-7 w-16 font-mono text-2xs"
        />
      </div>

      <div className="flex items-center gap-2">
        <Label
          htmlFor="shape-replicas"
          className="flex-1 font-mono text-2xs text-muted-foreground"
        >
          replicas
        </Label>
        <Input
          id="shape-replicas"
          type="number"
          min={1}
          value={replicas}
          onChange={(event) => setReplicas(Number(event.target.value))}
          className="h-7 w-16 font-mono text-2xs"
        />
      </div>

      <div className="flex items-center justify-between">
        <Label
          htmlFor="shape-single"
          className="font-mono text-2xs text-muted-foreground"
        >
          one fabric
        </Label>
        <Switch
          id="shape-single"
          checked={single}
          onCheckedChange={setSingle}
        />
      </div>

      <div className="flex items-center justify-between">
        <Label
          htmlFor="shape-spread"
          className="font-mono text-2xs text-muted-foreground"
        >
          spread clusters
        </Label>
        <Switch
          id="shape-spread"
          checked={spread}
          onCheckedChange={setSpread}
        />
      </div>

      <div className="flex gap-1">
        <Button
          size="sm"
          onClick={build}
          className="h-6 flex-1 font-mono text-2xs"
        >
          pick up
        </Button>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setOpen(false)}
          className="h-6 px-2 font-mono text-2xs"
        >
          cancel
        </Button>
      </div>
    </div>
  )
}
