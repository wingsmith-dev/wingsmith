import {
  ResourceGlyph,
  type ResourceKind,
} from "@/components/layout/resource-icon"
import { cn } from "@/lib/utils"

/**
 * The leading cell of a table row: the resource's tile beside whatever names it.
 * The tile is the same one cards and headers use, so a row scanned out of
 * context still says what kind of thing it is.
 */
export function ResourceCell({
  kind,
  children,
  className,
}: React.PropsWithChildren<{ kind: ResourceKind; className?: string }>) {
  return (
    <div className={cn("flex items-center gap-2.5", className)}>
      <ResourceGlyph kind={kind} size="xs" />
      <div className="min-w-0">{children}</div>
    </div>
  )
}
