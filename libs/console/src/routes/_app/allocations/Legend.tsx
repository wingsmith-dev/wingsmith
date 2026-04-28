import { cn } from "@/lib/utils"

const HATCH =
  "bg-[repeating-linear-gradient(45deg,currentColor_0px,currentColor_1px,transparent_1px,transparent_4px)]"

/**
 * What the cells mean. Colour is reserved for workloads, so every state a
 * device can be in has to be told by texture instead — dashed for free, hatched
 * for unusable, banded for partitioned.
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
          "bg-destructive/20 text-destructive/70 ring-1 ring-destructive/40 ring-inset",
          HATCH
        )}
      />
      <Item
        label="cordoned"
        face={cn(
          "bg-muted text-muted-foreground/50 ring-1 ring-border ring-inset",
          HATCH
        )}
      />
      <Item label="split" bands />
    </div>
  )
}

function Item({
  label,
  face,
  bands = false,
}: {
  label: string
  face?: string
  bands?: boolean
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
      <span className="font-mono text-2xs text-muted-foreground/60">
        {label}
      </span>
    </span>
  )
}
