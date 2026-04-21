import { Badge } from "@/components/ui/badge"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import { LEVEL_CLAIM } from "@/modules/benchmarks/display"
import type { BenchmarkLevel } from "@/modules/benchmarks/types"
import { Link } from "@tanstack/react-router"

/**
 * Three visibly different badges, because the three are different claims.
 *
 * A deployment result and an endpoint result put side by side in one colour
 * read as two rows of one table, which is the misreading that ends in somebody
 * averaging them. The tooltip carries what the level actually measures, so the
 * claim is one hover away from every number rather than in a document.
 */
const LEVEL_STYLES: Record<BenchmarkLevel, string> = {
  deployment:
    "border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300",
  workload:
    "border-violet-500/40 bg-violet-500/10 text-violet-700 dark:text-violet-300",
  endpoint:
    "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300",
}

export function LevelBadge({
  level,
  className,
}: {
  level: BenchmarkLevel
  className?: string
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge
          variant="outline"
          className={cn("font-mono text-2xs", LEVEL_STYLES[level], className)}
        >
          {level}
        </Badge>
      </TooltipTrigger>
      <TooltipContent className="max-w-64 text-2xs">
        Measures {LEVEL_CLAIM[level]}. Not comparable with a result taken at
        another level.
      </TooltipContent>
    </Tooltip>
  )
}

/**
 * The one target a request names, linked to whichever collection owns it.
 *
 * A level with no id renders as nothing rather than as a dead link: an
 * endpoint-level request whose endpoint has since been deleted still has a
 * record worth reading.
 */
export function TargetLink({
  level,
  target,
  className,
}: {
  level: BenchmarkLevel
  target: string
  className?: string
}) {
  const style = cn("font-mono text-xs hover:underline", className)
  if (!target) return <span className="text-muted-foreground/40">—</span>
  if (level === "endpoint")
    return (
      <Link to="/endpoints/$endpointId" params={{ endpointId: target }} className={style}>
        {target}
      </Link>
    )
  if (level === "workload")
    return (
      <Link to="/workloads/$workloadId" params={{ workloadId: target }} className={style}>
        {target}
      </Link>
    )
  return (
    <Link
      to="/deployments/$deploymentId"
      params={{ deploymentId: target }}
      className={style}
    >
      {target}
    </Link>
  )
}
