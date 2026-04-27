import { DataTable } from "@/components/layout/data-table"
import { MetricsSection } from "@/components/metrics/metrics-section"
import { PageHeader } from "@/components/layout/page-header"
import { nodeColumns } from "@/components/supply/columns"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { formatError } from "@/lib/format"
import { usePool } from "@/modules/supply/queries"
import { useNodes } from "@/modules/supply/queries"
import { IconArrowLeft } from "@tabler/icons-react"
import { Link, useParams } from "@tanstack/react-router"
import { PoolCard } from "../PoolCard"

export function PoolDetailPage() {
  const { poolId } = useParams({ from: "/_app/pools/$poolId" })
  const { data: pool, isLoading, error } = usePool(poolId)
  const nodes = useNodes({ pool: poolId })

  if (error) {
    return (
      <div className="flex flex-col">
        <PageHeader title="Pool" />
        <div className="p-10 text-center text-sm text-destructive">
          {formatError(error, "Failed to load pool")}
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col">
      <PageHeader
        title={pool?.name ?? "Pool"}
        description={pool ? `${pool.id} · ${pool.device_class}` : undefined}
        actions={
          <Button asChild variant="outline">
            <Link to="/pools">
              <IconArrowLeft className="size-4" />
              All pools
            </Link>
          </Button>
        }
      />

      <div className="space-y-4 p-4">
        {isLoading && <Skeleton className="h-56 w-full" />}
        {pool && <PoolCard pool={pool} />}

        {pool && <MetricsSection scope="pool" refId={pool.id} />}

        <div>
          <h2 className="mb-2 font-mono text-2xs tracking-[0.18em] text-muted-foreground/50 uppercase">
            Observed members
          </h2>
          <DataTable
            columns={nodeColumns({ showPool: false })}
            list={nodes}
            rowKey={(node) => node.id}
            empty="This pool has no nodes yet — declared, not yet provisioned."
          />
        </div>
      </div>
    </div>
  )
}
