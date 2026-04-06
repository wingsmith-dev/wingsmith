import { DeleteDialog } from "@/components/layout/destructive"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { formatError } from "@/lib/format"
import { useDeletePool, usePools } from "@/modules/supply/queries"
import type { Pool } from "@/modules/supply/types"
import { PoolCard } from "@/routes/_app/pools/PoolCard"
import { PoolForm } from "@/routes/_app/pools/PoolForm"
import { useState } from "react"
import { toast } from "sonner"

/**
 * The one declared thing on a cluster page, and so the only tab with writes on
 * it. Nodes and devices are observed and cannot be added or removed here — a
 * pool's node bounds are how "I want more nodes" is actually expressed.
 */
export function ClusterPoolsPanel({
  clusterId,
  creating,
  onCreatingChange,
}: {
  clusterId: string
  creating: boolean
  onCreatingChange: (creating: boolean) => void
}) {
  const list = usePools({ cluster: clusterId })
  const remove = useDeletePool()
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
    <div className="space-y-4">
      {list.isLoading &&
        Array.from({ length: 2 }, (_, i) => (
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

      {list.items && list.items.length === 0 && !list.isLoading && (
        <p className="py-10 text-center text-sm text-muted-foreground">
          No pools declared on this cluster.
        </p>
      )}

      {list.items && list.items.length > 0 && (
        <div className="flex items-center justify-between gap-4 px-1">
          <span className="font-mono text-2xs text-muted-foreground/60">
            {list.loaded} loaded{list.hasNextPage ? " · more available" : ""}
          </span>
          {list.hasNextPage && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => list.fetchNextPage()}
              disabled={list.isFetchingNextPage}
            >
              {list.isFetchingNextPage ? "Loading…" : "Load more"}
            </Button>
          )}
        </div>
      )}

      {creating && (
        <PoolForm
          open
          defaultCluster={clusterId}
          onOpenChange={(open) => !open && onCreatingChange(false)}
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
