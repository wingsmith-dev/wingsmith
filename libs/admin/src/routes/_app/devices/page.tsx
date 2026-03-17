import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { PageHeader } from "@/components/layout/page-header"
import { DevicesTable } from "@/components/supply/devices-table"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { useClusters } from "@/modules/clusters/queries"
import { useDevices, useTopologyDomains } from "@/modules/supply/queries"
import { useNavigate, useSearch } from "@tanstack/react-router"
import { GroupedDevices } from "./GroupedDevices"
import { TopologySummary } from "./TopologySummary"

const HEALTH = [
  { value: "healthy", label: "Healthy" },
  { value: "degraded", label: "Degraded" },
  { value: "failed", label: "Failed" },
  { value: "unknown", label: "Unknown" },
]

const ALLOCATION = [
  { value: "true", label: "Allocated" },
  { value: "false", label: "Idle" },
]

export function DevicesPage() {
  const search = useSearch({ from: "/_app/devices" })
  const navigate = useNavigate({ from: "/devices" })
  const { data: clusters } = useClusters()
  const { data: topology } = useTopologyDomains(search.cluster)
  const { data, isLoading, error } = useDevices({
    cluster_id: search.cluster,
    health: search.health,
    topology_domain_id: search.domain,
    allocated: search.allocated ? search.allocated === "true" : undefined,
  })

  const set =
    (key: "cluster" | "health" | "domain" | "allocated") => (value: string) =>
      navigate({
        search: (prev) => ({
          ...prev,
          [key]: value === ALL ? undefined : value,
        }),
      })

  const grouped = search.group === true

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Devices"
        description="observed · the schedulable unit — health, ECC, fabric, allocation"
      />

      <div className="space-y-4 p-4">
        <TopologySummary domains={topology?.items ?? []} />

        <div className="flex flex-wrap items-center gap-2">
          <FilterSelect
            label="Cluster"
            value={search.cluster ?? ALL}
            onChange={set("cluster")}
            options={(clusters?.items ?? []).map((c) => ({
              value: c.id,
              label: c.display_name,
            }))}
          />
          <FilterSelect
            label="Health"
            value={search.health ?? ALL}
            onChange={set("health")}
            options={HEALTH}
          />
          <FilterSelect
            label="Allocation"
            value={search.allocated ?? ALL}
            onChange={set("allocated")}
            options={ALLOCATION}
          />
          <FilterSelect
            label="Domain"
            value={search.domain ?? ALL}
            onChange={set("domain")}
            options={(topology?.items ?? []).map((d) => ({
              value: d.id,
              label: d.id,
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
              checked={grouped}
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

        <div className="font-mono text-2xs text-muted-foreground/60">
          {data?.count ?? 0} devices
        </div>

        {grouped && data && !isLoading ? (
          <GroupedDevices devices={data.items} domains={topology?.items ?? []} />
        ) : (
          <DevicesTable
            devices={data?.items}
            isLoading={isLoading}
            error={error}
            empty="No devices match these filters."
          />
        )}
      </div>
    </div>
  )
}
