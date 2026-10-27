import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { cn } from "@/lib/utils"
import { brickColor } from "@/modules/board/palette"
import { metrics } from "@/modules/board/sandbox"
import { useBoardStore } from "@/modules/board/store"
import type { Board, Brick, Move } from "@/modules/board/types"
import {
  IconArrowRight,
  IconCircleMinus,
  IconCirclePlus,
} from "@tabler/icons-react"
import { useMemo } from "react"

/**
 * The plan, and what it did to the fleet's shape.
 *
 * Every number is a before and an after, because a plan is only ever better or
 * worse than what is already there. `largest block` is the one to watch: free
 * devices can go up while the biggest thing you could still place goes down, and
 * that trade is invisible in a total.
 */
export function PlanPanel({
  board,
  planned,
}: {
  board: Board | undefined
  planned: Board | undefined
}) {
  const moves = useBoardStore((s) => s.moves)
  const undo = useBoardStore((s) => s.undo)
  const clear = useBoardStore((s) => s.clear)

  const before = useMemo(() => (board ? metrics(board) : null), [board])
  const after = useMemo(() => (planned ? metrics(planned) : null), [planned])

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center justify-between text-sm">
          Plan
          <span className="font-mono text-2xs text-muted-foreground">
            {moves.length} {moves.length === 1 ? "change" : "changes"}
          </span>
        </CardTitle>
      </CardHeader>

      <CardContent className="space-y-3">
        {moves.length === 0 ? (
          <p className="font-mono text-2xs text-muted-foreground">
            Place a brick or drag an allocation to start one.
          </p>
        ) : (
          <ul className="space-y-1">
            {group(moves).map((entry) =>
              entry.kind === "batch" ? (
                <li key={entry.key}>
                  <BatchRow batch={entry} board={planned} />
                </li>
              ) : (
                <li key={entry.key}>
                  <MoveRow move={entry.move} board={planned} />
                </li>
              )
            )}
          </ul>
        )}

        {before && after && (
          <div className="space-y-1 border-t pt-2.5">
            <Row
              label="free devices"
              before={before.free}
              after={after.free}
              up
            />
            <Row
              label="largest block"
              before={before.largest_block}
              after={after.largest_block}
              up
            />
            <Row
              label="whole fabrics free"
              before={before.whole_fabrics_free}
              after={after.whole_fabrics_free}
              up
            />
            <Row
              label="split fabrics"
              before={before.fabrics_split}
              after={after.fabrics_split}
              up={false}
            />
          </div>
        )}

        {moves.length > 0 && (
          <div className="flex gap-1">
            <Button
              variant="outline"
              size="sm"
              onClick={undo}
              className="h-6 flex-1 font-mono text-2xs"
            >
              undo
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={clear}
              className="h-6 flex-1 font-mono text-2xs"
            >
              discard plan
            </Button>
          </div>
        )}

        <p className="font-mono text-2xs leading-relaxed text-muted-foreground">
          A plan stays here. Allocations are observed state with one writer (the
          cluster), so there is nothing to submit and nothing to apply.
        </p>
      </CardContent>
    </Card>
  )
}

interface Batch {
  kind: "batch"
  key: string
  brick: Brick
  moves: Extract<Move, { kind: "place" }>[]
}

type Entry = Batch | { kind: "single"; key: string; move: Move }

/**
 * A planned workload reads as one entry, not as six.
 *
 * Its replicas are collapsed into a row that says how many landed and where,
 * because that is the shape of the decision that produced them. Everything a
 * person did by hand stays its own row.
 */
function group(moves: Move[]): Entry[] {
  const entries: Entry[] = []
  const batches = new Map<string, Batch>()

  for (const move of moves) {
    const batch = move.kind === "place" ? move.batch : undefined
    if (move.kind !== "place" || !batch) {
      entries.push({ kind: "single", key: move.id, move })
      continue
    }
    const found = batches.get(batch)
    if (found) {
      found.moves.push(move)
      continue
    }
    const created: Batch = {
      kind: "batch",
      key: batch,
      brick: move.brick,
      moves: [move],
    }
    batches.set(batch, created)
    entries.push(created)
  }
  return entries
}

function BatchRow({
  batch,
  board,
}: {
  batch: Batch
  board: Board | undefined
}) {
  const color = brickColor(batch.brick.workload ?? batch.brick.key)
  const clusters = new Set<string>()
  const fabrics: string[] = []
  for (const move of batch.moves) {
    for (const name of fabricsOf(board, move.devices)) {
      if (!fabrics.includes(name)) fabrics.push(name)
    }
    const first = board?.devices.get(move.devices[0])
    if (first) clusters.add(first.device.cluster_name)
  }
  const devices = batch.moves.reduce(
    (total, move) => total + move.devices.length,
    0
  )

  return (
    <div className="flex items-start gap-2 rounded-md bg-muted/30 px-1.5 py-1">
      <IconCirclePlus className="mt-0.5 size-3 shrink-0 text-success" />
      <span
        className={cn("mt-1 size-2 shrink-0 rounded-[2px]", color.swatch)}
      />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-mono text-2xs">
          {batch.brick.label}
          <span className="text-muted-foreground">
            {" "}
            · {batch.moves.length}{" "}
            {batch.moves.length === 1 ? "replica" : "replicas"} · {devices} dev
          </span>
        </span>
        <span className="block truncate font-mono text-2xs text-muted-foreground">
          {[...clusters].join(", ")} · {fabrics.length}{" "}
          {fabrics.length === 1 ? "fabric" : "fabrics"}
        </span>
      </span>
    </div>
  )
}

function MoveRow({ move, board }: { move: Move; board: Board | undefined }) {
  const label =
    move.kind === "place" ? move.brick.label : move.holder.workload_name
  const key =
    move.kind === "place"
      ? (move.brick.workload ?? move.brick.key)
      : move.holder.workload
  const color = brickColor(key)

  const to = move.kind === "release" ? [] : fabricsOf(board, move.devices)
  const from = move.kind === "place" ? [] : fabricsOf(board, move.from)

  return (
    <div className="flex items-start gap-2 rounded-md bg-muted/30 px-1.5 py-1">
      <span className="mt-0.5 shrink-0">
        {move.kind === "release" ? (
          <IconCircleMinus className="size-3 text-warning" />
        ) : move.kind === "move" ? (
          <IconArrowRight className="size-3 text-primary" />
        ) : (
          <IconCirclePlus className="size-3 text-success" />
        )}
      </span>
      <span
        className={cn("mt-1 size-2 shrink-0 rounded-[2px]", color.swatch)}
      />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-mono text-2xs">{label}</span>
        <span className="block truncate font-mono text-2xs text-muted-foreground">
          {move.kind === "place" && `→ ${to.join(", ")}`}
          {move.kind === "move" && `${from.join(", ")} → ${to.join(", ")}`}
          {move.kind === "release" && `released from ${from.join(", ")}`}
        </span>
      </span>
    </div>
  )
}

/** Device ids read back as the fabrics they sit in, deduplicated. */
function fabricsOf(board: Board | undefined, devices: string[]): string[] {
  if (!board) return []
  const names = new Set<string>()
  for (const id of devices) {
    const fabric = board.fabrics.get(board.fabric_of.get(id) ?? "")
    if (fabric) names.add(fabric.label)
  }
  return [...names]
}

function Row({
  label,
  before,
  after,
  up,
}: {
  label: string
  before: number
  after: number
  /** Whether more is better. Split fabrics is the one where it is not. */
  up: boolean
}) {
  const delta = after - before
  const good = up ? delta > 0 : delta < 0
  return (
    <div className="flex items-baseline gap-2 font-mono text-2xs">
      <span className="flex-1 text-muted-foreground">{label}</span>
      <span className="text-muted-foreground tabular-nums">{before}</span>
      <IconArrowRight className="size-2.5 text-muted-foreground" />
      <span className="w-6 text-right tabular-nums">{after}</span>
      <span
        className={cn(
          "w-8 text-right tabular-nums",
          delta === 0
            ? "text-muted-foreground"
            : good
              ? "text-success"
              : "text-warning"
        )}
      >
        {delta === 0 ? "—" : delta > 0 ? `+${delta}` : delta}
      </span>
    </div>
  )
}
