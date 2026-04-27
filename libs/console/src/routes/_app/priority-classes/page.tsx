import { CardGrid } from "@/components/binding/card-grid"
import { DeleteDialog } from "@/components/layout/destructive"
import { PageHeader } from "@/components/layout/page-header"
import { Button } from "@/components/ui/button"
import { formatError } from "@/lib/format"
import {
  useDeletePriorityClass,
  usePriorityClasses,
} from "@/modules/binding/queries"
import type { PriorityClass } from "@/modules/binding/types"
import { PriorityClassCard } from "./PriorityClassCard"
import { PriorityClassForm } from "./PriorityClassForm"
import { useState } from "react"
import { toast } from "sonner"

/**
 * Cards rather than a table. This collection is bounded by design — a handful
 * of classes across the whole plane — and each one is two switches and a
 * number, which a row flattens into columns that read like an accident.
 */
export function PriorityClassesPage() {
  const list = usePriorityClasses()
  const remove = useDeletePriorityClass()
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<PriorityClass | undefined>()
  const [doomed, setDoomed] = useState<PriorityClass | undefined>()

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Priority classes"
        description="declared · what preempts what"
        actions={
          <Button onClick={() => setCreating(true)}>New priority class</Button>
        }
      />
      <div className="space-y-4 p-4">
        <CardGrid
          list={list}
          rowKey={(priority) => priority.id}
          empty="No priority classes."
        >
          {(priority) => (
            <PriorityClassCard
              priorityClass={priority}
              onEdit={() => setEditing(priority)}
              onDelete={() => setDoomed(priority)}
            />
          )}
        </CardGrid>
      </div>

      {creating && (
        <PriorityClassForm
          open
          onOpenChange={(open) => !open && setCreating(false)}
        />
      )}
      {editing && (
        <PriorityClassForm
          key={editing.revision}
          open
          priorityClass={editing}
          onOpenChange={(open) => !open && setEditing(undefined)}
        />
      )}
      <DeleteDialog
        icon="priority-class"
        open={!!doomed}
        onOpenChange={(open) => !open && setDoomed(undefined)}
        kind="priority class"
        name={doomed?.name ?? ""}
        note="Workloads and projects naming it lose their priority."
        error={remove.error}
        pending={remove.isPending}
        onConfirm={() =>
          doomed &&
          remove.mutate(doomed.id, {
            onSuccess: () => {
              setDoomed(undefined)
              toast.success("Priority class deleted", {
                description: doomed.name,
              })
            },
            onError: (e) =>
              toast.error(formatError(e, "Could not delete class")),
          })
        }
      />
    </div>
  )
}
