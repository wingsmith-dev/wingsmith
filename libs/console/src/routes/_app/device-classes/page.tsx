import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { PageHeader } from "@/components/layout/page-header"
import { ResourceIcon } from "@/components/layout/resource-icon"
import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { formatError } from "@/lib/format"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { gibibytes } from "@/modules/supply/display"
import { useDeviceClasses } from "@/modules/supply/queries"
import type { DeviceClass } from "@/modules/supply/types"
import { IconAlertTriangle } from "@tabler/icons-react"
import { Link, useNavigate, useSearch } from "@tanstack/react-router"

const PRESENCE = [
  { value: "true", label: "In the fleet" },
  { value: "false", label: "Not owned" },
]

/**
 * What hardware exists, and how much of it the fleet has.
 *
 * The two halves are intentionally in one table instead of two pages. A class
 * with no devices is known but absent from the fleet; a class with devices and
 * no catalog entry is already running and has never been named. Splitting them would hide the second behind a filter nobody sets.
 */
export function DeviceClassesPage() {
  const search = useSearch({ from: "/_app/device-classes" })
  const navigate = useNavigate({ from: "/device-classes" })
  const { data: clusters } = useClusterOptions()

  const { data, isLoading, error } = useDeviceClasses({
    cluster: search.cluster,
    architecture: search.architecture,
    present:
      search.present === undefined ? undefined : search.present === "true",
  })

  const set =
    (key: "cluster" | "architecture" | "present") => (value: string) =>
      navigate({
        search: (prev) => ({
          ...prev,
          [key]: value === ALL ? undefined : value,
        }),
      })

  const classes = data ?? []
  const unnamed = classes.filter((entry) => !entry.in_catalog)
  const architectures = [
    ...new Set(classes.map((c) => c.architecture).filter(Boolean)),
  ].sort()

  return (
    <div className="flex flex-col">
      <PageHeader
        title="GPU models"
        description="the shared device-class dictionary, and how much of each the fleet holds"
      />

      <div className="space-y-4 p-4">
        {unnamed.length > 0 && <Unnamed classes={unnamed} />}

        <div className="flex flex-wrap items-center gap-2">
          <FilterSelect
            label="Cluster"
            value={search.cluster ?? ALL}
            onChange={set("cluster")}
            options={(clusters ?? []).map((cluster) => ({
              value: cluster.id,
              label: clusterLabel(cluster),
            }))}
          />
          <FilterSelect
            label="Architecture"
            value={search.architecture ?? ALL}
            onChange={set("architecture")}
            options={architectures.map((a) => ({ value: a, label: a }))}
          />
          <FilterSelect
            label="Presence"
            value={search.present ?? ALL}
            onChange={set("present")}
            options={PRESENCE}
          />
        </div>

        {error ? (
          <Card size="sm" className="px-3 py-3 text-sm text-destructive">
            {formatError(error, "Could not load the device catalog")}
          </Card>
        ) : isLoading ? (
          <Skeleton className="h-64 w-full" />
        ) : (
          <Catalog classes={classes} cluster={search.cluster} />
        )}
      </div>
    </div>
  )
}

/**
 * Hardware in the fleet that the contract table does not name. It resolved to
 * the slug of whatever the vendor called it and is being scheduled onto, which
 * makes it the one row on this page that is a task.
 */
function Unnamed({ classes }: { classes: DeviceClass[] }) {
  return (
    <Card
      size="sm"
      className="gap-1.5 border-amber-500/30 bg-amber-500/5 px-3 py-3"
    >
      <div className="flex items-center gap-2 text-xs">
        <IconAlertTriangle className="size-4 shrink-0 text-warning" />
        <span className="font-medium">
          {classes.length === 1
            ? "One class in the fleet has no catalog entry"
            : `${classes.length} classes in the fleet have no catalog entry`}
        </span>
      </div>
      <p className="text-2xs text-muted-foreground">
        These resolved to the slug of the vendor string and are being scheduled
        onto with no memory, form factor or architecture known. Add them to{" "}
        <code className="font-mono">libs/packages/contract/devices.json</code>{" "}
        and release the plane and the operator together.
      </p>
      <div className="flex flex-wrap gap-1.5 pt-1">
        {classes.map((entry) => (
          <Badge
            key={entry.name}
            variant="outline"
            className="border-amber-500/30 font-mono text-2xs"
          >
            {entry.name} · {entry.inventory.devices}
          </Badge>
        ))}
      </div>
    </Card>
  )
}

function Catalog({
  classes,
  cluster,
}: {
  classes: DeviceClass[]
  cluster?: string
}) {
  if (classes.length === 0) {
    return (
      <Card size="sm" className="px-3 py-8 text-center text-sm">
        <span className="text-muted-foreground">
          No device classes match these filters.
        </span>
      </Card>
    )
  }

  return (
    <div className="overflow-x-auto rounded-lg border bg-card">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Model</TableHead>
            <TableHead>Architecture</TableHead>
            <TableHead className="text-right">Memory</TableHead>
            <TableHead className="hidden lg:table-cell">Form</TableHead>
            <TableHead className="text-right">Devices</TableHead>
            <TableHead className="text-right">Healthy</TableHead>
            <TableHead className="text-right">Schedulable</TableHead>
            <TableHead className="text-right">Nodes</TableHead>
            <TableHead>Clusters</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {classes.map((entry) => (
            <Row key={entry.name} entry={entry} cluster={cluster} />
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

function Row({ entry, cluster }: { entry: DeviceClass; cluster?: string }) {
  const { inventory } = entry
  const impaired = inventory.devices - inventory.healthy
  return (
    <TableRow>
      <TableCell>
        <div className="flex items-start gap-2">
          <ResourceIcon
            kind="device-class"
            className="mt-0.5 size-3.5 text-muted-foreground"
          />
          <div className="min-w-0">
            <Link
              to="/devices"
              search={{ class: entry.name, cluster }}
              className="block hover:underline"
            >
              <span className="text-xs">{entry.label}</span>
              <span className="block truncate font-mono text-2xs text-muted-foreground">
                {entry.name}
              </span>
            </Link>
            {!entry.in_catalog && (
              <span className="font-mono text-2xs text-warning">
                not in the catalog
              </span>
            )}
          </div>
        </div>
      </TableCell>
      <TableCell className="font-mono text-2xs text-muted-foreground">
        {entry.architecture || "—"}
      </TableCell>
      <TableCell className="text-right font-mono text-2xs tabular-nums">
        {entry.memory_mib ? gibibytes(entry.memory_mib) : "—"}
      </TableCell>
      <TableCell className="hidden font-mono text-2xs text-muted-foreground lg:table-cell">
        {entry.form_factor || "—"}
      </TableCell>
      <TableCell className="text-right font-mono text-xs tabular-nums">
        {inventory.devices || (
          <span className="text-muted-foreground">none</span>
        )}
      </TableCell>
      <TableCell className="text-right font-mono text-2xs tabular-nums">
        {inventory.devices === 0 ? (
          <span className="text-muted-foreground">—</span>
        ) : (
          <span className={impaired > 0 ? "text-warning" : ""}>
            {inventory.healthy}
          </span>
        )}
      </TableCell>
      <TableCell className="text-right font-mono text-2xs tabular-nums">
        {inventory.devices === 0 ? (
          <span className="text-muted-foreground">—</span>
        ) : (
          inventory.allocatable
        )}
      </TableCell>
      <TableCell className="text-right font-mono text-2xs tabular-nums">
        {inventory.nodes || <span className="text-muted-foreground">—</span>}
      </TableCell>
      <TableCell className="font-mono text-2xs text-muted-foreground">
        {inventory.clusters.length > 0 ? (
          inventory.clusters.join(", ")
        ) : (
          <span className="text-muted-foreground">not owned</span>
        )}
      </TableCell>
    </TableRow>
  )
}
