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
import { useProjectOptions } from "@/modules/binding/queries"
import {
  useArtifactFitment,
  useArtifactOptions,
} from "@/modules/demand/queries"
import type { ArtifactFit } from "@/modules/demand/types"
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
 * The bricks: what could run, in the three forms the plane can describe it.
 *
 * A **workload** is a commitment — it has a scale, spread rules and replicas
 * already holding devices. A **recipe** is a shape with no scale, which is what
 * makes it the right brick: a recipe declares `device_class`, `device_count` and
 * whether they must share a fabric, and an artifact declares none of that. An
 * **artifact** sits one step further back and answers a different question —
 * fitment says which cards could hold these weights at all — so its rows arrive
 * here as candidate shapes rather than as placements.
 *
 * Where the answer comes from matters and is labelled. A brick naming a recipe
 * and a project is a question the plane can answer, and its verdicts are
 * authoritative: only the plane sees quota, priority, policy, residency and what
 * would have to be preempted. Everything else is the board's own arithmetic over
 * devices and fabrics, marked `est.` because it can be right about hardware and
 * wrong about admission.
 */
export function Tray({ board }: { board: Board | undefined }) {
  const { bricks, shapes, isLoading } = useBricks()
  const armed = useBoardStore((s) => s.armed)
  const arm = useBoardStore((s) => s.arm)
  const custom = useBoardStore((s) => s.custom)

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
        <p className="font-mono text-2xs text-muted-foreground/60">
          {armed
            ? `${armed.label} in hand · click a device or fabric to place one replica`
            : "click or drag to place one replica · the bolt plans the rest"}
        </p>
      </CardHeader>

      <CardContent className="space-y-3 px-2">
        {isLoading && (
          <p className="px-2 font-mono text-2xs text-muted-foreground/50">
            Loading…
          </p>
        )}

        {!isLoading && (
          <Group
            label="Workloads"
            empty="Nothing is declared yet. Place a recipe below to see where it would go."
            count={bricks.length}
          >
            {bricks.map((brick) => (
              <BrickRow key={brick.key} brick={brick} {...rowProps} />
            ))}
          </Group>
        )}

        {!isLoading && (
          <Group
            label="Recipes"
            empty="No recipes. Build a shape by hand instead."
            count={shapes.length}
          >
            {shapes.length > 0 && (
              <div className="flex items-center gap-2 px-1.5 pb-1">
                <Label className="font-mono text-2xs text-muted-foreground/60">
                  as project
                </Label>
                <Select
                  value={asProject ?? ""}
                  onValueChange={setProject}
                  disabled={!projects?.length}
                >
                  <SelectTrigger
                    size="sm"
                    className="h-6 flex-1 font-mono text-2xs"
                  >
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
            )}
            {shapes.map((shape) => (
              <ShapeRow
                key={shape.key}
                shape={shape}
                project={asProject}
                {...rowProps}
              />
            ))}
          </Group>
        )}

        <Group label="Artifacts" empty="" count={1}>
          <ArtifactShapes {...rowProps} />
        </Group>

        <Group label="Hand-built" empty="" count={1}>
          {custom && <BrickRow brick={custom} {...rowProps} />}
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
      <div className="px-1.5 font-mono text-2xs tracking-[0.15em] text-muted-foreground/50 uppercase">
        {label}
      </div>
      {count === 0 && empty ? (
        <p className="px-1.5 font-mono text-2xs leading-relaxed text-muted-foreground/40">
          {empty}
        </p>
      ) : null}
      {children}
    </div>
  )
}

/**
 * A recipe, with the one number it does not carry: how many of it.
 *
 * Replicas live on the workload, so asking a recipe where it would fit means
 * saying how many first, and who for — `POST /placement/preview` takes a project
 * because quota and policy are per project.
 */
function ShapeRow({
  shape,
  project,
  board,
  assignments,
}: {
  shape: Brick
  project: string | null
  board: Board | undefined
  assignments: Assignments
}) {
  const [replicas, setReplicas] = useState(1)

  const brick = useMemo(
    () => ({
      ...shape,
      project,
      replicas: Math.max(1, replicas),
      label: replicas > 1 ? `${replicas} × ${shape.label}` : shape.label,
    }),
    [shape, project, replicas]
  )

  return (
    <div className="flex items-start gap-1">
      <BrickRow
        brick={brick}
        board={board}
        assignments={assignments}
        className="flex-1"
      />
      <Input
        type="number"
        min={1}
        value={replicas}
        aria-label={`Replicas of ${shape.label}`}
        title="replicas"
        onClick={(event) => event.stopPropagation()}
        onChange={(event) => setReplicas(Number(event.target.value))}
        className="mt-1 h-6 w-11 shrink-0 px-1 text-center font-mono text-2xs"
      />
    </div>
  )
}

/**
 * One brick, and where it would go.
 *
 * Collapsed, it shows the board's own count: replicas held over replicas wanted,
 * and how many more the devices could take. Expanding asks the plane, and its
 * answer replaces the estimate — lazily, because each call costs the plane a pass
 * over quota, policy and every cluster's capacity, and a tray of thirty recipes
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
  const colour = brickColor(brick.workload ?? brick.key)
  const askable = !!brick.recipe && (!!brick.workload || !!brick.project)

  const expansion = useWorkloadExpansion(
    brick.workload,
    open && askable && !!brick.workload
  )
  const preview = usePlacementPreview(
    {
      recipe: brick.recipe,
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
      toast.warning(`${brick.label} — ${summary}`, {
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
        onClick={() => arm(armed?.key === brick.key ? null : brick)}
        className={cn(
          "flex min-w-0 cursor-grab items-center gap-2 rounded-md px-1.5 py-1.5 transition-colors hover:bg-accent/50 active:cursor-grabbing",
          armed?.key === brick.key &&
            "bg-primary/10 ring-1 ring-primary/40 ring-inset"
        )}
      >
        <IconGripVertical className="size-3 shrink-0 text-muted-foreground/40" />
        <span
          className={cn("size-2.5 shrink-0 rounded-[2px]", colour.swatch)}
        />
        <span className="min-w-0 flex-1">
          <span className="block truncate font-mono text-2xs">
            {brick.label}
          </span>
          <span className="block truncate font-mono text-2xs text-muted-foreground/50">
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
                  ? "text-muted-foreground/40"
                  : plan.placed === plan.wanted
                    ? "text-emerald-600 dark:text-emerald-400"
                    : plan.placed === 0
                      ? "text-destructive"
                      : "text-amber-600 dark:text-amber-400"
              )}
              title={
                authoritative
                  ? "from the plane's decision"
                  : "the board's own arithmetic — ignores quota, policy and preemption"
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
              ? `The board finds room for ${estimate.plan.placed} of ${estimate.plan.wanted} on devices alone. The decision above is the authority — it also weighs quota, policy and preemption, and asks for a replica's devices contiguously.`
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
 * so itself — a fit is not a promise a replica can be placed, because contiguity
 * inside a topology domain decides that — so these rows are shapes to try on the
 * board, and they carry no recipe to ask the plane about.
 */
function ArtifactShapes({
  board,
  assignments,
}: {
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
        <p className="px-1.5 font-mono text-2xs text-muted-foreground/50">
          sizing…
        </p>
      )}
      {fitment.data?.note && (
        <p className="px-1.5 font-mono text-2xs leading-relaxed text-muted-foreground/40">
          {fitment.data.note}
        </p>
      )}
      {artifact && !fitment.isLoading && bricks.length === 0 && (
        <p className="px-1.5 font-mono text-2xs leading-relaxed text-muted-foreground/40">
          Nothing in the catalog holds these weights at{" "}
          {fitment.data?.context_length ?? "?"} tokens.
        </p>
      )}
      {bricks.map((brick) => (
        <BrickRow
          key={brick.key}
          brick={brick}
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
      fit.domain_count > 1
        ? `tp${fit.tensor_parallel} · pp${fit.pipeline_parallel} over ${fit.domain_count} domains`
        : `tp${fit.tensor_parallel} · one domain`,
      fit.kv_counted
        ? `${fit.context_length} ctx`
        : "weights only · window uncosted",
    ].join(" · "),
    workload: null,
    recipe: null,
    project: null,
    priority_class: null,
    project_slug: "sizing",
    device_class: fit.device_class,
    device_count: fit.device_count,
    /**
     * The fit says how the replica is laid out, so this is read rather than
     * guessed: tensor-parallel ranks share a domain, and pipeline stages cross
     * one. A replica spanning domains is a normal answer for a large model, not
     * a degraded one, so `domain_count` above 1 is what says it may spread.
     */
    requires_single_topology_domain: fit.domain_count <= 1,
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
 * eight of something would fit before there is a recipe to ask on behalf of.
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
      recipe: null,
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
