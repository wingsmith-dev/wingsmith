import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { unitLabel } from "@/modules/metrics/format"
import { metricGroups, type MetricFamily } from "@/modules/metrics/catalog"
import { IconAdjustments } from "@tabler/icons-react"

/**
 * Which of a family's metrics a panel charts.
 *
 * Every registered name in the family is offered, grouped and with its unit, so
 * the two throughput measurements are never a single row somebody reads as one
 * setting. A name the store returned no series for is offered disabled rather
 * than hidden: "we do not measure that here" and "there is no such metric" are
 * different answers.
 */
export function MetricPicker({
  family,
  selected,
  available,
  onChange,
}: {
  family: MetricFamily
  selected: readonly string[]
  /** Names the metrics store answered with. Everything else is offered disabled. */
  available: readonly string[]
  onChange: (names: string[]) => void
}) {
  const groups = metricGroups(family)
  const chosen = new Set(selected)
  const answered = new Set(available)

  const toggle = (name: string) => {
    const next = new Set(chosen)
    if (next.has(name)) next.delete(name)
    else next.add(name)
    onChange(
      groups
        .flatMap((group) =>
          group.metrics.filter((metric) => next.has(metric.name))
        )
        .map((metric) => metric.name)
    )
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button size="sm" variant="outline">
          <IconAdjustments className="size-4" />
          Metrics
          <span className="font-mono text-2xs text-muted-foreground">
            {chosen.size}
          </span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="max-h-96 w-80 overflow-y-auto"
      >
        {groups.map((group, index) => (
          <div key={group.group}>
            {index > 0 && <DropdownMenuSeparator />}
            <DropdownMenuLabel className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
              {group.label}
            </DropdownMenuLabel>
            {group.metrics.map((metric) => (
              <DropdownMenuCheckboxItem
                key={metric.name}
                checked={chosen.has(metric.name)}
                disabled={!answered.has(metric.name)}
                onSelect={(event) => event.preventDefault()}
                onCheckedChange={() => toggle(metric.name)}
              >
                <span className="flex min-w-0 flex-col">
                  <span className="truncate">{metric.title}</span>
                  <span className="font-mono text-2xs text-muted-foreground">
                    {metric.name} · {unitLabel(metric.unit)}
                    {answered.has(metric.name) ? "" : " · no series"}
                  </span>
                </span>
              </DropdownMenuCheckboxItem>
            ))}
          </div>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
