import { Panel } from "@/components/layout/panel"
import { ResourceIcon } from "@/components/layout/resource-icon"
import { StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { usePolicies, usePriorityClasses } from "@/modules/binding/queries"
import type { Policy, PriorityClass } from "@/modules/binding/types"
import { gibibytes } from "@/modules/supply/display"
import { useDeviceClasses } from "@/modules/supply/queries"
import type { DeviceClass } from "@/modules/supply/types"
import { IconArrowUpRight } from "@tabler/icons-react"
import { Link } from "@tanstack/react-router"

/**
 * The shared device-class dictionary, summarized. The full catalog, with the
 * fleet-inventory joins, filters and the unnamed-class alarm, is its own
 * surface at `/device-classes`, reached from the header here.
 */
export function GpuModelsSection() {
  const { data, isLoading } = useDeviceClasses()
  const classes = data ?? []

  return (
    <Panel
      id="gpu-models"
      title="GPU models"
      description="hardware types Hangar knows how to schedule"
      action={
        <Link
          to="/device-classes"
          className="inline-flex items-center gap-1 font-mono text-2xs text-primary hover:underline"
        >
          Full catalog
          <IconArrowUpRight className="size-3.5" />
        </Link>
      }
    >
      {isLoading ? (
        <Skeleton className="h-48 w-full" />
      ) : classes.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">
          No device classes in the catalog yet.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Model</TableHead>
                <TableHead>Memory</TableHead>
                <TableHead>Architecture</TableHead>
                <TableHead className="hidden lg:table-cell">Form</TableHead>
                <TableHead>In use</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {classes.map((entry) => (
                <GpuModelRow key={entry.name} entry={entry} />
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </Panel>
  )
}

function GpuModelRow({ entry }: { entry: DeviceClass }) {
  const clusters = entry.inventory.clusters.length
  const devices = entry.inventory.devices
  return (
    <TableRow>
      <TableCell>
        <div className="flex items-start gap-2">
          <ResourceIcon
            kind="device-class"
            className="mt-0.5 size-3.5 shrink-0 text-muted-foreground"
          />
          <div className="min-w-0">
            <Link
              to="/devices"
              search={{ class: entry.name }}
              className="block hover:text-primary hover:underline"
            >
              <span className="inline-flex items-center gap-1.5 text-xs">
                {entry.label}
                <IconArrowUpRight className="size-3 text-muted-foreground" />
              </span>
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
      <TableCell className="font-mono text-2xs tabular-nums">
        {entry.memory_mib ? gibibytes(entry.memory_mib) : "—"}
      </TableCell>
      <TableCell className="font-mono text-2xs text-muted-foreground">
        {entry.architecture || "—"}
      </TableCell>
      <TableCell className="hidden font-mono text-2xs text-muted-foreground lg:table-cell">
        {entry.form_factor || "—"}
      </TableCell>
      <TableCell className="font-mono text-2xs">
        {devices > 0 ? (
          <span>
            {clusters} {clusters === 1 ? "cluster" : "clusters"} · {devices}
          </span>
        ) : (
          <span className="text-muted-foreground">not in use</span>
        )}
      </TableCell>
    </TableRow>
  )
}

/**
 * The plane's scheduling classes, summarized. The full surface, with create,
 * edit and the total-ordering guard, is its own page at `/priority-classes`,
 * reached from the header here.
 */
export function PriorityClassesSection() {
  const list = usePriorityClasses()
  const classes = (list.items ?? []).slice().sort((a, b) => b.value - a.value)

  return (
    <Panel
      id="priority"
      title="Priority classes"
      description="scheduling order and preemption behavior"
      action={
        <Link
          to="/priority-classes"
          className="inline-flex items-center gap-1 font-mono text-2xs text-primary hover:underline"
        >
          Manage
          <IconArrowUpRight className="size-3.5" />
        </Link>
      }
    >
      {list.isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : classes.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">
          No priority classes defined yet.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Class</TableHead>
                <TableHead className="text-right">Value</TableHead>
                <TableHead>Description</TableHead>
                <TableHead>Preemptible</TableHead>
                <TableHead>Preempts</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {classes.map((c) => (
                <PriorityClassRow key={c.id} entry={c} />
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      <p className="mt-3 text-xs text-muted-foreground">
        Priority decides who wins when quota is contended; consumption itself is
        tracked under Access › Quotas.
      </p>
    </Panel>
  )
}

function PriorityClassRow({ entry }: { entry: PriorityClass }) {
  return (
    <TableRow>
      <TableCell className="font-mono text-2xs">
        {entry.name}
        {entry.is_default && (
          <Badge variant="outline" className="ml-2 font-mono text-2xs">
            default
          </Badge>
        )}
      </TableCell>
      <TableCell className="text-right font-mono text-2xs tabular-nums">
        {entry.value}
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {entry.description || "—"}
      </TableCell>
      <TableCell className="font-mono text-2xs text-muted-foreground">
        {entry.preemptible ? "yes" : "no"}
      </TableCell>
      <TableCell className="font-mono text-2xs text-muted-foreground">
        {entry.preempts ? "yes" : "no"}
      </TableCell>
    </TableRow>
  )
}

/**
 * The plane's placement guardrails, summarized. The full surface, with the
 * allow/deny lists, rules and subject attachments, is its own page at
 * `/policies`, reached from the header here.
 */
export function PoliciesSection() {
  const list = usePolicies()
  const policies = list.items ?? []

  return (
    <Panel
      id="policies"
      title="Policies"
      description="guardrails applied at admission and scheduling time"
      action={
        <Link
          to="/policies"
          className="inline-flex items-center gap-1 font-mono text-2xs text-primary hover:underline"
        >
          Manage
          <IconArrowUpRight className="size-3.5" />
        </Link>
      }
    >
      {list.isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : policies.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">
          No policies defined yet.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Policy</TableHead>
                <TableHead>Description</TableHead>
                <TableHead>Mode</TableHead>
                <TableHead>Scope</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {policies.map((p) => (
                <PolicyRow key={p.id} entry={p} />
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </Panel>
  )
}

function PolicyRow({ entry }: { entry: Policy }) {
  const scope =
    entry.attachments.length === 0
      ? "unattached"
      : entry.attachments
          .map((a) => a.subject_name || a.subject || a.subject_id)
          .join(", ")

  return (
    <TableRow>
      <TableCell className="font-mono text-2xs">{entry.name}</TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {entry.description || "—"}
      </TableCell>
      <TableCell>
        <StatusLabel tone={entry.enabled ? "ok" : "neutral"}>
          {entry.enabled ? "Enforcing" : "Disabled"}
        </StatusLabel>
      </TableCell>
      <TableCell className="font-mono text-2xs text-muted-foreground">
        {scope}
      </TableCell>
    </TableRow>
  )
}
