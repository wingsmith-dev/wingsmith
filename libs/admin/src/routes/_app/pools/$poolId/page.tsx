import { PageHeader } from "@/components/layout/page-header"
import { NodesTable } from "@/components/supply/nodes-table"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { usePool, usePoolNodes } from "@/modules/supply/queries"
import { IconArrowLeft } from "@tabler/icons-react"
import { Link, useParams } from "@tanstack/react-router"
import { PoolCard } from "../PoolCard"

export function PoolDetailPage() {
  const { poolId } = useParams({ from: "/_app/pools/$poolId" })
  const { data: pool, isLoading, error } = usePool(poolId)
  const nodes = usePoolNodes(poolId)

  if (error) {
    return (
      <div className="flex flex-col">
        <PageHeader title="Pool" />
        <div className="p-10 text-center text-sm text-destructive">
          {error instanceof Error ? error.message : "Failed to load pool"}
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col">
      <PageHeader
        title={pool?.name ?? "Pool"}
        description={
          pool ? `${pool.id} · ${pool.declared.device_class}` : undefined
        }
        actions={
          <Button asChild variant="outline" size="sm">
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

        <div>
          <h2 className="mb-2 font-mono text-2xs uppercase tracking-[0.18em] text-muted-foreground/50">
            Observed members
          </h2>
          <NodesTable
            nodes={nodes.data?.items}
            isLoading={nodes.isLoading}
            error={nodes.error}
            showPool={false}
            empty="This pool has no nodes yet — declared, not yet provisioned."
          />
        </div>
      </div>
    </div>
  )
}
