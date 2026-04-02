import { DeleteDialog } from "@/components/layout/destructive"
import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { PageHeader } from "@/components/layout/page-header"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { formatError } from "@/lib/format"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { useDeletePool, usePools } from "@/modules/supply/queries"
import type { Pool } from "@/modules/supply/types"
import { useNavigate, useSearch } from "@tanstack/react-router"
import { useState } from "react"
import { toast } from "sonner"
import { PoolCard } from "./PoolCard"
import { PoolForm } from "./PoolForm"

export function PoolsPage() {
  const search = useSearch({ from: "/_app/pools" })
  const navigate = useNavigate({ from: "/pools" })
  const { data: clusters } = useClusterOptions()
  const list = usePools({
    cluster: search.cluster,
    class: search.class,
    enabled:
      search.enabled === undefined ? undefined : search.enabled === "true",
  })
  const remove = useDeletePool()

  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Pool | null>(null)
  const [deleting, setDeleting] = useState<Pool | null>(null)

  const confirmDelete = () => {
    if (!deleting) return
    remove.mutate(deleting.id, {
      onSuccess: () => {
        toast.success("Pool deleted", { description: deleting.name })
        setDeleting(null)
      },
      onError: (error) =>
        toast.error(formatError(error, "Could not delete pool")),
    })
  }

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Pools"
        description="declared provisioning spec · observed membership beside it"
        actions={
          <Button size="sm" onClick={() => setCreating(true)}>
            New pool
          </Button>
        }
      />

      <div className="space-y-4 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <FilterSelect
            label="Cluster"
            value={search.cluster ?? ALL}
            onChange={(value) =>
              navigate({
                search: (prev) => ({
                  ...prev,
                  cluster: value === ALL ? undefined : value,
                }),
              })
            }
            options={(clusters ?? []).map((cluster) => ({
              value: cluster.id,
              label: clusterLabel(cluster),
            }))}
          />
          <div className="ml-auto font-mono text-2xs text-muted-foreground/60">
            {list.loaded} loaded{list.hasNextPage ? " · more available" : ""}
          </div>
        </div>

        {list.isLoading &&
          Array.from({ length: 3 }, (_, i) => (
            <Skeleton key={i} className="h-56 w-full" />
          ))}

        {!!list.error && (
          <p className="py-10 text-center text-sm text-destructive">
            {formatError(list.error, "Failed to load pools")}
          </p>
        )}

        {list.items?.map((pool) => (
          <PoolCard
            key={pool.id}
            pool={pool}
            onEdit={() => setEditing(pool)}
            onDelete={() => setDeleting(pool)}
          />
        ))}

        {list.items && list.items.length === 0 && (
          <p className="py-10 text-center text-sm text-muted-foreground">
            No pools declared.
          </p>
        )}

        {list.hasNextPage && (
          <Button
            variant="outline"
            className="w-full"
            onClick={() => list.fetchNextPage()}
            disabled={list.isFetchingNextPage}
          >
            {list.isFetchingNextPage ? "Loading…" : "Load more"}
          </Button>
        )}
      </div>

      {creating && (
        <PoolForm
          open
          defaultCluster={search.cluster}
          onOpenChange={(open) => !open && setCreating(false)}
        />
      )}
      {editing && (
        <PoolForm
          key={editing.revision}
          pool={editing}
          open
          onOpenChange={(open) => !open && setEditing(null)}
        />
      )}

      <DeleteDialog
        icon="pool"
        open={!!deleting}
        onOpenChange={(open) => !open && setDeleting(null)}
        kind="pool"
        name={deleting?.name ?? ""}
        note="Observed membership stops being reported against it."
        error={remove.error}
        pending={remove.isPending}
        onConfirm={confirmDelete}
      />
    </div>
  )
}
