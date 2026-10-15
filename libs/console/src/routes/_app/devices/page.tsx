import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { DataTable } from "@/components/layout/data-table"
import { PageHeader } from "@/components/layout/page-header"
import { deviceColumns } from "@/components/supply/columns"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { DEVICE_HEALTHS } from "@/modules/supply/display"
import {
  useAllTopologyDomains,
  useDeviceClasses,
  useDevices,
} from "@/modules/supply/queries"
import { useNavigate, useSearch } from "@tanstack/react-router"
import { useState } from "react"
import { CountTile } from "@/components/layout/count-tile"
import { GroupedDevices, type GroupMode } from "./GroupedDevices"
import { TopologySummary } from "./TopologySummary"

const ALLOCATABLE = [
  { value: "true", label: "Allocatable" },
  { value: "false", label: "Unschedulable" },
]

type DeviceView = GroupMode | "individual"

export function DevicesPage() {
  const search = useSearch({ from: "/_app/devices" })
  const navigate = useNavigate({ from: "/devices" })
  const { data: clusters } = useClusterOptions()
  const topology = useAllTopologyDomains({ cluster: search.cluster })
  const [view, setView] = useState<DeviceView>("class")

  const list = useDevices({
    cluster: search.cluster,
    node: search.node,
    pool: search.pool,
    class: search.class,
    health: search.health,
    topology_domain: search.domain,
    allocatable:
      search.allocatable === undefined
        ? undefined
        : search.allocatable === "true",
  })

  const set =
    (key: "cluster" | "health" | "domain" | "allocatable" | "class") =>
    (value: string) =>
      navigate({
        search: (prev) => ({
          ...prev,
          [key]: value === ALL ? undefined : value,
        }),
      })

  /**
   * From the catalog scoped to what this fleet holds, not from the loaded page.
   * Deriving it from `list.items` made the filter depend on which classes
   * happened to be in the first page — narrowing to a class could remove it
   * from its own dropdown.
   */
  const {
    data: classes,
    isLoading: classesLoading,
    error: classesError,
  } = useDeviceClasses({
    cluster: search.cluster,
    present: true,
  })

  const totals = (classes ?? []).reduce(
    (acc, c) => {
      acc.devices += c.inventory.devices
      acc.free += c.inventory.allocatable
      acc.allocated += c.inventory.allocated
      acc.healthy += c.inventory.healthy
      return acc
    },
    { devices: 0, free: 0, allocated: 0, healthy: 0 }
  )
  const faulted = totals.devices - totals.healthy
  const allocated = totals.allocated

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Devices"
        description="observed · the schedulable unit — health, ECC, fabric, allocatability"
      />

      <div className="space-y-4 p-4">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <CountTile
            kind="device"
            label="Devices"
            value={totals.devices}
            to="/devices"
            isLoading={classesLoading}
            error={classesError}
          />
          <CountTile
            kind="device"
            label="Allocated"
            value={allocated}
            detail={`/ ${totals.devices}`}
            to="/devices"
            isLoading={classesLoading}
            error={classesError}
          />
          <CountTile
            kind="device"
            label="Free"
            value={totals.free}
            to="/devices"
            notes={[{ label: "allocatable", tone: "ok" }]}
            isLoading={classesLoading}
            error={classesError}
          />
          <CountTile
            kind="device"
            label="Faulted"
            value={faulted}
            to="/devices"
            notes={
              faulted > 0 ? [{ label: "not healthy", tone: "warn" }] : undefined
            }
            isLoading={classesLoading}
            error={classesError}
          />
        </div>

        <TopologySummary
          domains={topology.data}
          isLoading={topology.isLoading}
        />

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
            label="Class"
            value={search.class ?? ALL}
            onChange={set("class")}
            options={(classes ?? []).map((c) => ({
              value: c.name,
              label: c.label,
            }))}
          />
          <FilterSelect
            label="Health"
            value={search.health ?? ALL}
            onChange={set("health")}
            options={DEVICE_HEALTHS.map((h) => ({ value: h, label: h }))}
          />
          <FilterSelect
            label="Schedulable"
            value={search.allocatable ?? ALL}
            onChange={set("allocatable")}
            options={ALLOCATABLE}
          />
          <FilterSelect
            label="Domain"
            value={search.domain ?? ALL}
            onChange={set("domain")}
            options={(topology.data ?? []).map((domain) => ({
              value: domain.id,
              label: domain.identifier,
            }))}
          />
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3">
          <Tabs
            value={view}
            onValueChange={(value) => setView(value as DeviceView)}
          >
            <TabsList>
              <TabsTrigger value="class">By model</TabsTrigger>
              <TabsTrigger value="cluster">By cluster</TabsTrigger>
              <TabsTrigger value="topology">By topology</TabsTrigger>
              <TabsTrigger value="individual">Individual</TabsTrigger>
            </TabsList>
          </Tabs>

          {view === "class" && (
            <div className="flex flex-wrap items-center gap-3 font-mono text-2xs text-muted-foreground">
              <Swatch className="bg-primary" label="allocated" />
              <Swatch className="bg-destructive" label="fault" />
              <Swatch className="bg-secondary" label="free" />
            </div>
          )}
        </div>

        {view !== "individual" && list.items ? (
          <>
            <GroupedDevices devices={list.items} by={view} />
            {list.hasNextPage && (
              <button
                type="button"
                onClick={() => list.fetchNextPage()}
                className="w-full rounded-lg border border-dashed py-2 font-mono text-2xs text-muted-foreground hover:bg-accent/40"
              >
                {list.isFetchingNextPage
                  ? "Loading…"
                  : `Load more — ${list.loaded} devices grouped so far`}
              </button>
            )}
          </>
        ) : (
          <DataTable
            columns={deviceColumns()}
            list={list}
            rowKey={(device) => device.id}
            empty="No devices match these filters."
          />
        )}
      </div>
    </div>
  )
}

function Swatch({ className, label }: { className: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`h-2.5 w-2 rounded-[2px] ${className}`} />
      {label}
    </span>
  )
}
