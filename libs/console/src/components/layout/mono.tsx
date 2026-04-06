import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

/** A prefixed id, rendered as the opaque string it is. */
export function Id({
  value,
  className,
}: {
  value: string
  className?: string
}) {
  return (
    <span
      className={cn("font-mono text-2xs text-muted-foreground/60", className)}
    >
      {value}
    </span>
  )
}

export function Labels({ labels }: { labels: Record<string, string> }) {
  const entries = Object.entries(labels)
  if (entries.length === 0) {
    return (
      <span className="font-mono text-2xs text-muted-foreground/40">none</span>
    )
  }
  return (
    <div className="flex flex-wrap gap-1">
      {entries.map(([key, value]) => (
        <Badge key={key} variant="outline" className="font-mono text-2xs">
          {key}={value}
        </Badge>
      ))}
    </div>
  )
}

export function Dash({ children }: { children: React.ReactNode }) {
  if (children === null || children === undefined || children === "") {
    return <span className="text-muted-foreground/40">—</span>
  }
  return <>{children}</>
}

/**
 * A declared value and its observed counterpart, kept side by side. They are
 * never merged: `desired` is what we asked for and `observed` is what came
 * back, and one nullable column cannot say both.
 */
export function VersionPair({
  desired,
  observed,
  behind,
}: {
  desired: string
  observed: string | null
  behind: boolean
}) {
  return (
    <span className="font-mono text-2xs">
      <span className={behind ? "text-amber-600 dark:text-amber-400" : ""}>
        {observed ?? "—"}
      </span>
      <span className="text-muted-foreground/40"> / {desired}</span>
    </span>
  )
}
