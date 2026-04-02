import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { DataTable } from "@/components/layout/data-table"
import { PageHeader } from "@/components/layout/page-header"
import { deviceColumns } from "@/components/supply/columns"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { DEVICE_HEALTHS } from "@/modules/supply/display"
import { useAllTopologyDomains, useDevices } from "@/modules/supply/queries"
import { useNavigate, useSearch } from "@tanstack/react-router"
import { GroupedDevices } from "./GroupedDevices"
import { TopologySummary } from "./TopologySummary"

const ALLOCATABLE = [
  { value: "true", label: "Allocatable" },
  { value: "false", label: "Unschedulable" },
]

export function DevicesPage() {
  const search = useSearch({ from: "/_app/devices" })
  const navigate = useNavigate({ from: "/devices" })
  const { data: clusters } = useClusterOptions()
  const topology = useAllTopologyDomains({ cluster: search.cluster })

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

  const classes = [
    ...new Set((list.items ?? []).map((d) => d.device_class)),
  ].sort()

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Devices"
        description="observed · the schedulable unit — health, ECC, fabric, allocatability"
      />

      <div className="space-y-4 p-4">
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
            options={classes.map((c) => ({ value: c, label: c }))}
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

          <div className="ml-auto flex items-center gap-2">
            <Label
              htmlFor="group-topology"
              className="font-mono text-2xs text-muted-foreground/70"
            >
              Group by topology domain
            </Label>
            <Switch
              id="group-topology"
              checked={search.group === true}
              onCheckedChange={(checked) =>
                navigate({
                  search: (prev) => ({
                    ...prev,
                    group: checked ? true : undefined,
                  }),
                })
              }
            />
          </div>
        </div>

        {search.group && list.items ? (
          <>
            <GroupedDevices devices={list.items} />
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
