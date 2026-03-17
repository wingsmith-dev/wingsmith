import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { PageHeader } from "@/components/layout/page-header"
import { Skeleton } from "@/components/ui/skeleton"
import { useClusters } from "@/modules/clusters/queries"
import { usePools } from "@/modules/supply/queries"
import { useNavigate, useSearch } from "@tanstack/react-router"
import { PoolCard } from "./PoolCard"

export function PoolsPage() {
  const search = useSearch({ from: "/_app/pools" })
  const navigate = useNavigate({ from: "/pools" })
  const { data: clusters } = useClusters()
  const { data, isLoading, error } = usePools(search.cluster)

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Pools"
        description="declared · provisioning spec, with observed membership beside it"
      />

      <div className="space-y-4 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <FilterSelect
            label="Cluster"
            value={search.cluster ?? ALL}
            onChange={(value) =>
              navigate({
                search: () => ({
                  cluster: value === ALL ? undefined : value,
                }),
              })
            }
            options={(clusters?.items ?? []).map((c) => ({
              value: c.id,
              label: c.display_name,
            }))}
          />
          <div className="ml-auto font-mono text-2xs text-muted-foreground/60">
            {data?.count ?? 0} pools
          </div>
        </div>

        {isLoading &&
          Array.from({ length: 3 }, (_, i) => (
            <Skeleton key={i} className="h-56 w-full" />
          ))}

        {error && (
          <p className="py-10 text-center text-sm text-destructive">
            {error instanceof Error ? error.message : "Failed to load pools"}
          </p>
        )}

        {data?.items.map((pool) => <PoolCard key={pool.id} pool={pool} />)}

        {data && data.items.length === 0 && (
          <p className="py-10 text-center text-sm text-muted-foreground">
            No pools declared.
          </p>
        )}
      </div>
    </div>
  )
}
