import { ResourceIcon, type ResourceKind } from "@/components/layout/resource-icon"
import { cn } from "@/lib/utils"

/**
 * The leading cell of a table row: the resource's glyph beside whatever names
 * it. The icon is the same one the sidebar and the page header use, so a row
 * scanned out of context still says what kind of thing it is.
 */
export function ResourceCell({
  kind,
  children,
  className,
}: React.PropsWithChildren<{ kind: ResourceKind; className?: string }>) {
  return (
    <div className={cn("flex items-center gap-2", className)}>
      <ResourceIcon kind={kind} className="size-3.5 text-muted-foreground/50" />
      <div className="min-w-0">{children}</div>
    </div>
  )
}
