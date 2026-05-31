import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { PageHeader } from "@/components/layout/page-header"
import { RefreshButton } from "@/components/layout/refresh-button"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { cn } from "@/lib/utils"
import { formatError } from "@/lib/format"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { useFleetBoard } from "@/modules/board/queries"
import { useBoardStore } from "@/modules/board/store"
import { useDeviceClasses } from "@/modules/supply/queries"
import { useNavigate, useSearch } from "@tanstack/react-router"
import { IconEye, IconFlask } from "@tabler/icons-react"
import { useEffect } from "react"
import { FleetBoard } from "./FleetBoard"
import { Inspector } from "./Inspector"
import { Legend } from "./Legend"
import { PlanPanel } from "./PlanPanel"
import { Tray } from "./Tray"
import type { BoardMode } from "./route"

/** Every cache entry the board reads, as prefixes for the refresh button. */
const BOARD_KEYS = [
  ["clusters"],
  ["nodes"],
  ["devices"],
  ["topology-domains"],
  ["allocations"],
  ["workloads"],
  ["placement", "reservations"],
]

/**
 * Clicking the background puts the inspector down.
 *
 * Opt-out rather than opt-in: anything that means something to click carries
 * `data-keeps-selection` and everything else — card padding, the gaps between
 * fabric rows, the space beside a short row — is background. The alternative is
 * a handler on every leaf that stops propagation, which is the same rule spread
 * over thirty places and forgotten in one.
 */
function clearOnBackground(select: (selection: null) => void) {
  return (event: React.MouseEvent<HTMLElement>) => {
    const target = event.target as HTMLElement | null
    if (target?.closest("[data-keeps-selection]")) return
    select(null)
  }
}

/**
 * The board: every device the fleet holds, nested the way capacity actually
 * nests — region, cluster, fabric, device, slice — painted by what holds it.
 *
 * Two things happen here and they are deliberately the same screen. Reading the
 * board answers "where is everything right now"; the sandbox answers "what if it
 * were somewhere else", by laying a plan over the observed allocations and
 * repainting from the result. Nothing in the sandbox is ever sent: allocations
 * are observed state with exactly one writer, and it is not the console.
 */
export function AllocationsPage() {
  const search = useSearch({ from: "/_app/allocations" })
  const navigate = useNavigate({ from: "/allocations" })
  const mode: BoardMode = search.mode ?? "live"
  const groupRegions = search.regions === true

  const moves = useBoardStore((s) => s.moves)
  const select = useBoardStore((s) => s.select)
  const clear = useBoardStore((s) => s.clear)
  const arm = useBoardStore((s) => s.arm)

  const { data: clusters } = useClusterOptions()
  const { data: classes } = useDeviceClasses({ present: true })

  /**
   * Both boards at once. Every number in the plan panel is a difference, and a
   * difference needs the thing it is a difference from — so the live board is
   * still built while the sandbox is being played with.
   */
  const { board, planned, isLoading, error } = useFleetBoard({
    cluster: search.cluster,
    class: search.class,
    moves: mode === "sandbox" ? moves : [],
  })

  useEffect(() => {
    if (search.device) select({ kind: "device", id: search.device })
  }, [search.device, select])

  useEffect(() => {
    if (mode === "live") arm(null)
  }, [mode, arm])

  /**
   * Escape puts the brick down, then clears the selection. Two presses rather
   * than one because a brick in hand changes what clicking the board does, and
   * that is the state worth being able to leave first.
   */
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return
      if (useBoardStore.getState().armed) arm(null)
      else select(null)
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [arm, select])

  const set = (key: "cluster" | "class") => (value: string) =>
    navigate({
      search: (prev) => ({ ...prev, [key]: value === ALL ? undefined : value }),
    })

  const setMode = (next: BoardMode) => {
    if (next === "live") clear()
    navigate({
      search: (prev) => ({
        ...prev,
        mode: next === "sandbox" ? "sandbox" : undefined,
      }),
    })
  }

  return (
    <div className="flex min-h-0 flex-col">
      <PageHeader
        title="Allocations"
        description={
          mode === "sandbox"
            ? "sandbox · a plan laid over observed allocations, sent nowhere"
            : "observed · devices committed together to one workload"
        }
        readActions={
          <div className="flex items-center gap-2">
            <ModeToggle mode={mode} onChange={setMode} />
            <RefreshButton keys={BOARD_KEYS} />
          </div>
        }
      />

      <div className="flex flex-wrap items-center gap-2 border-b bg-card/40 px-4 py-2">
        <FilterSelect
          label="Cluster"
          value={search.cluster ?? ALL}
          onChange={set("cluster")}
          options={(clusters ?? []).map((cluster) => ({
            value: cluster.id,
            label: clusterLabel(cluster),
          }))}
        />
        <FilterSelect
          label="Class"
          value={search.class ?? ALL}
          onChange={set("class")}
          options={(classes ?? []).map((entry) => ({
            value: entry.name,
            label: entry.label,
          }))}
        />
        <div className="flex items-center gap-2 pl-1">
          <Switch
            id="group-regions"
            checked={groupRegions}
            onCheckedChange={(checked) =>
              navigate({
                search: (prev) => ({
                  ...prev,
                  regions: checked ? true : undefined,
                }),
              })
            }
          />
          <Label
            htmlFor="group-regions"
            className="font-mono text-2xs text-muted-foreground"
          >
            Group regions
          </Label>
        </div>

        <div className="ml-auto">
          <Legend />
        </div>
      </div>

      {error && (
        <div className="border-b bg-destructive/5 px-4 py-2 font-mono text-2xs text-destructive">
          {formatError(error)}
        </div>
      )}

      <div
        onClick={clearOnBackground(select)}
        className="grid flex-1 items-start gap-4 p-4 xl:grid-cols-[minmax(0,1fr)_21rem]"
      >
        <FleetBoard
          board={planned}
          isLoading={isLoading}
          mode={mode}
          groupRegions={groupRegions}
        />

        <aside
          data-keeps-selection
          className="flex flex-col gap-4 xl:sticky xl:top-4"
        >
          {mode === "sandbox" && <Tray board={planned} />}
          {mode === "sandbox" && <PlanPanel board={board} planned={planned} />}
          <Inspector board={planned} mode={mode} />
        </aside>
      </div>
    </div>
  )
}

function ModeToggle({
  mode,
  onChange,
}: {
  mode: BoardMode
  onChange: (mode: BoardMode) => void
}) {
  return (
    <div className="flex items-center rounded-md border bg-background p-0.5">
      <ModeButton
        active={mode === "live"}
        onClick={() => onChange("live")}
        icon={<IconEye className="size-3.5" />}
        label="Live"
      />
      <ModeButton
        active={mode === "sandbox"}
        onClick={() => onChange("sandbox")}
        icon={<IconFlask className="size-3.5" />}
        label="Sandbox"
      />
    </div>
  )
}

function ModeButton({
  active,
  onClick,
  icon,
  label,
}: {
  active: boolean
  onClick: () => void
  icon: React.ReactNode
  label: string
}) {
  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={onClick}
      className={cn(
        "h-6 gap-1.5 px-2 font-mono text-2xs",
        active && "bg-primary/10 text-primary hover:bg-primary/15"
      )}
    >
      {icon}
      {label}
    </Button>
  )
}
