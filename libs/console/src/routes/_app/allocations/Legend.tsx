import { cn } from "@/lib/utils"

export const HATCH =
  "bg-[repeating-linear-gradient(45deg,currentColor_0px,currentColor_1px,transparent_1px,transparent_4px)]"

/**
 * What the cells mean. Colour is reserved for workloads, so every state a
 * device can be in has to be told by texture instead — dashed for free, hatched
 * for unusable, banded for partitioned.
 *
 * `reserved` is the one entry whose cells are a guess. It is capacity a cluster
 * owes a deployment that has not bound anything yet, and the cluster has not
 * said which accelerators it will use — so the board picks plausible ones and
 * paints them in the workload's own colour, washed out and dashed to say they
 * are being claimed rather than held. The cells are indicative and the count in
 * the tally is exact; the hover text on every one of them says which is which.
 *
 * Its swatch is drawn twice on purpose — a cell and a bar. The cell is what to
 * look for on the board; the bar is the same fact on a cluster's utilisation
 * strip, where a promise is a proportion of capacity rather than a square.
 */
export function Legend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
      <Item
        label="free"
        face="bg-muted/30 ring-1 ring-dashed ring-border ring-inset"
      />
      <Item label="held" face="bg-primary/80" />
      <Item
        label="proposed"
        face="bg-primary/80 outline-2 outline-dashed outline-offset-[-3px] outline-background"
      />
      <Item
        label="fault"
        face={cn(
          "bg-destructive/20 text-destructive ring-1 ring-destructive/40 ring-inset",
          HATCH
        )}
      />
      <Item
        label="cordoned"
        face={cn(
          "bg-muted text-muted-foreground ring-1 ring-border ring-inset",
          HATCH
        )}
      />
      <Item label="split" bands />
      <Item
        label="reserved"
        face="bg-primary/25 ring-1 ring-dashed ring-primary/60 ring-inset"
        bar={
          <span
            className={cn("block size-full bg-primary/25 text-primary", HATCH)}
          />
        }
      />
    </div>
  )
}

/**
 * One key. `bar`, when given, is a second swatch drawn beside the cell — for the
 * one state that appears both as a device and as a share of a utilisation strip.
 */
function Item({
  label,
  face,
  bands = false,
  bar,
}: {
  label: string
  face?: string
  bands?: boolean
  bar?: React.ReactNode
}) {
  return (
    <span className="flex items-center gap-1.5">
      <span className="size-3.5 overflow-hidden rounded-[3px]">
        {bands ? (
          <span className="flex size-full flex-col gap-px">
            <span className="min-h-0 flex-1 rounded-[1px] bg-primary/80" />
            <span className="ring-dashed min-h-0 flex-1 rounded-[1px] bg-muted/40 ring-1 ring-border ring-inset" />
            <span className="min-h-0 flex-1 rounded-[1px] bg-primary/80" />
          </span>
        ) : (
          <span className={cn("block size-full", face)} />
        )}
      </span>
      {bar && (
        <span className="h-1.5 w-3.5 overflow-hidden rounded-full">{bar}</span>
      )}
      <span className="font-mono text-2xs text-muted-foreground">{label}</span>
    </span>
  )
}
