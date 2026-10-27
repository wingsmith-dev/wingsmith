import { Badge } from "@/components/ui/badge"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { StatusLabel } from "@/components/layout/status-dot"
import {
  LIMIT_SCOPE_LABEL,
  LIMIT_SCOPE_MEANING,
  ON_EXCEED_MEANING,
  beyondSafeInteger,
  formatLimit,
  shortfallLabel,
} from "@/modules/binding/display"
import type {
  LimitScope,
  OnExceed,
  ServingQuotaStandard,
} from "@/modules/binding/types"

/**
 * A committed limit, or the fact that nothing was committed on that axis.
 *
 * Null is uncommitted and is drawn as its own state, never as `0` and never as
 * the em dash that means "no value yet": an uncommitted axis is not limited at
 * all, which is the opposite of a limit of zero.
 */
export function Committed({
  value,
  unit,
}: {
  value: number | null
  unit?: string
}) {
  if (value === null) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="font-mono text-2xs text-warning">uncommitted</span>
        </TooltipTrigger>
        <TooltipContent className="max-w-64 text-2xs">
          Nothing was committed on this axis, so the gateway does not limit it.
          Not a limit of zero.
        </TooltipContent>
      </Tooltip>
    )
  }

  if (beyondSafeInteger(value)) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="font-mono text-xs text-destructive">
            too large to read
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-64 text-2xs">
          The plane sent a value past what a JavaScript number holds exactly.
          Read it with `hgr` instead of trusting a rounded figure here.
        </TooltipContent>
      </Tooltip>
    )
  }

  return (
    <span className="font-mono text-xs">
      {formatLimit(value)}
      {unit && (
        <span className="text-muted-foreground">
          {" "}
          {unit}
        </span>
      )}
    </span>
  )
}

/**
 * Whether the gateway refuses on this commitment or only counts it. Shadow mode
 * is the loudest thing on the screen: an operator who believes a
 * limit is live when it is not has sold a guarantee nothing upholds.
 */
export function EnforcementBadge({ enforced }: { enforced: boolean }) {
  if (enforced) return <StatusLabel tone="ok">enforced</StatusLabel>
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge
          variant="destructive"
          className="font-mono text-2xs tracking-wide uppercase"
        >
          shadow mode
        </Badge>
      </TooltipTrigger>
      <TooltipContent className="max-w-72 text-2xs">
        The gateway counts these limits and reports on them, and never refuses a
        request. Nothing upholds this commitment until enforcement is on.
      </TooltipContent>
    </Tooltip>
  )
}

/** How many of the rows loaded so far are counted but never enforced. */
export function ShadowNotice({
  shadowed,
  total,
}: {
  shadowed: number
  total: number
}) {
  if (shadowed === 0) return null
  return (
    <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
      {shadowed} of {total} loaded commitments are in shadow mode: counted and
      reported, never refused.
    </div>
  )
}

export function ScopeBadge({ scope }: { scope: LimitScope }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant="outline" className="font-mono text-2xs">
          {LIMIT_SCOPE_LABEL[scope]}
        </Badge>
      </TooltipTrigger>
      <TooltipContent className="max-w-72 text-2xs">
        {LIMIT_SCOPE_MEANING[scope]}
      </TooltipContent>
    </Tooltip>
  )
}

export function OnExceedBadge({
  onExceed,
  queueTimeoutMs,
}: {
  onExceed: OnExceed
  queueTimeoutMs: number | null
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="font-mono text-2xs text-muted-foreground">
          {onExceed}
          {onExceed === "queue" && queueTimeoutMs !== null && (
            <span className="text-muted-foreground"> · {queueTimeoutMs}ms</span>
          )}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-64 text-2xs">
        {ON_EXCEED_MEANING[onExceed]}
      </TooltipContent>
    </Tooltip>
  )
}

/**
 * Whether what was promised clears the acceptance standard's floors. It compares
 * declared against declared and is not a measurement of what the endpoint
 * delivered: that lives on the metrics surface and is never spelled like this.
 */
export function StandardBadge({
  standard,
}: {
  standard: ServingQuotaStandard
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge
          variant={standard.meets ? "outline" : "secondary"}
          className="font-mono text-2xs"
        >
          {standard.meets
            ? "meets standard"
            : `short: ${standard.shortfalls.map(shortfallLabel).join(", ")}`}
        </Badge>
      </TooltipTrigger>
      <TooltipContent className="max-w-72 text-2xs">
        The acceptance standard's floors, compared against what was declared,
        not against anything measured. An uncommitted field is a shortfall.
      </TooltipContent>
    </Tooltip>
  )
}
