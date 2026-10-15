import { Card } from "@/components/ui/card"
import { cn } from "@/lib/utils"

/**
 * The headline row every workbench view opens with.
 *
 * Lifted out of the workload page so the suite pages read the same. Five
 * columns because that is what the load-curve row needs; a kind with fewer
 * facts to state leaves the row short rather than padding it out with numbers
 * nobody asked for.
 */
export function StatRow({ children }: React.PropsWithChildren) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
      {children}
    </div>
  )
}

export function StatTile({
  label,
  value,
  tone,
  small,
}: {
  label: string
  value: string
  tone?: "ok" | "warn" | "danger"
  small?: boolean
}) {
  return (
    <Card size="sm" className="gap-1.5 px-3">
      <div className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
        {label}
      </div>
      <div
        className={cn(
          "font-mono leading-none tabular-nums",
          small ? "text-sm" : "text-xl",
          tone === "ok" && "text-success",
          tone === "warn" && "text-warning",
          tone === "danger" && "text-red-600 dark:text-red-400"
        )}
      >
        {value}
      </div>
    </Card>
  )
}
