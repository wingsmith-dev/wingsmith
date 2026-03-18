import { RESOURCE_ICONS, type ResourceKind } from "@/lib/resource-kinds"
import { cn } from "@/lib/utils"

export type { ResourceKind }

export function ResourceIcon({
  kind,
  className,
}: {
  kind: ResourceKind
  className?: string
}) {
  const Icon = RESOURCE_ICONS[kind]
  return <Icon className={cn("size-4 shrink-0", className)} />
}

/** The icon in its tinted tile, for page headers and card corners. */
export function ResourceGlyph({
  kind,
  className,
  size = "md",
}: {
  kind: ResourceKind
  className?: string
  size?: "sm" | "md"
}) {
  return (
    <div
      className={cn(
        "flex shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary",
        size === "sm" ? "size-7" : "size-9",
        className
      )}
    >
      <ResourceIcon kind={kind} className={size === "sm" ? "size-4" : "size-5"} />
    </div>
  )
}
