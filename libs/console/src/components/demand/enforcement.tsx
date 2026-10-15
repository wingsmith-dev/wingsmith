import { Badge } from "@/components/ui/badge"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import type { Enforcement } from "@/modules/demand/display"

const ENFORCEMENT: Record<
  Enforcement,
  { label: string; hint: string; className: string }
> = {
  obeyed: {
    label: "obeyed",
    hint: "Rendered and honoured. Changing it changes what actually runs.",
    className: "border-emerald-500/40 bg-emerald-500/10 text-success",
  },
  asserted: {
    label: "asserted",
    hint: "A claim. Nothing renders it and nothing checks it — a conformance run is what can prove it wrong.",
    className: "border-amber-500/40 bg-amber-500/10 text-warning",
  },
  disclosure: {
    label: "disclosure",
    hint: "Documented and enforced by nothing, in any configuration. Do not present it to a tenant as a limit.",
    className: "border-amber-500/40 bg-amber-500/10 text-warning",
  },
  conditional: {
    label: "under enforce",
    hint: "A term only while the gateway runs with its enforce capability on. Declared and compared against observation otherwise.",
    className: "border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-400",
  },
}

/**
 * What is actually done with a declared field, spelled on the field itself.
 *
 * The whole reason `serving` and `capabilities` are two blocks — and the reason
 * half of an endpoint's surface is not a term — is that one of them is obeyed
 * and the other is only said. Rendering them identically is what turns a
 * disclosure into a promise somebody makes on our behalf.
 */
export function EnforcementBadge({
  kind,
  className,
}: {
  kind: Enforcement
  className?: string
}) {
  const entry = ENFORCEMENT[kind]
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge
          variant="outline"
          className={cn("font-mono text-2xs", entry.className, className)}
        >
          {entry.label}
        </Badge>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs text-2xs">
        {entry.hint}
      </TooltipContent>
    </Tooltip>
  )
}

/**
 * A titled group inside a form dialog, carrying the one badge that says what
 * happens to everything in it.
 *
 * `plain` is for the dialog that IS this one section: the frame and the heading
 * would repeat the dialog's own, and a box drawn inside a box says nothing
 * except that two components were composed. The badge stays either way — it is
 * the part that carries meaning.
 */
export function DeclaredSection({
  title,
  kind,
  hint,
  plain,
  children,
}: React.PropsWithChildren<{
  title: string
  kind: Enforcement
  hint: string
  plain?: boolean
}>) {
  return (
    <section className={cn("space-y-3", !plain && "rounded-md border p-3")}>
      <div className="space-y-1">
        <div className="flex items-center gap-2">
          {!plain && <h3 className="text-xs font-medium">{title}</h3>}
          <EnforcementBadge kind={kind} />
        </div>
        <p className="text-2xs text-muted-foreground">{hint}</p>
      </div>
      {children}
    </section>
  )
}
