import { Badge } from "@/components/ui/badge"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"

/** Shown on the row, never hidden: a discarded run is in no aggregate anywhere. */
export function DiscardBadge() {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge
          variant="outline"
          className="ml-2 border-dashed font-mono text-2xs text-muted-foreground"
        >
          discarded
        </Badge>
      </TooltipTrigger>
      <TooltipContent className="max-w-64 text-2xs">
        A warm-up, excluded from its request's claim. Kept so it can be read
        when the warm-up was itself what failed, and counted by no aggregate.
      </TooltipContent>
    </Tooltip>
  )
}
