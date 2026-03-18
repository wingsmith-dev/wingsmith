import { DataTable, type Column } from "@/components/layout/data-table"
import { Dash, Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { Badge } from "@/components/ui/badge"
import { usePriorityClasses } from "@/modules/binding/queries"
import type { PriorityClass } from "@/modules/binding/types"

const columns: Column<PriorityClass>[] = [
  {
    key: "name",
    header: "Class",
    cell: (priority) => (
      <span className="block">
        <span className="font-mono text-xs">{priority.name}</span>
        <Id value={priority.id} className="ml-2" />
      </span>
    ),
  },
  {
    key: "value",
    header: "Value",
    align: "right",
    cell: (priority) => (
      <span className="font-mono text-xs">{priority.value}</span>
    ),
  },
  {
    key: "preempts",
    header: "Preempts others",
    cell: (priority) => (
      <span className="font-mono text-2xs">{priority.preempts ? "yes" : "no"}</span>
    ),
  },
  {
    key: "preemptible",
    header: "Can be preempted",
    cell: (priority) => (
      <span className="font-mono text-2xs">
        {priority.preemptible ? "yes" : "no"}
      </span>
    ),
  },
  {
    key: "default",
    header: "Default",
    cell: (priority) =>
      priority.is_default ? (
        <Badge variant="secondary" className="font-mono text-2xs">
          default
        </Badge>
      ) : (
        <span className="font-mono text-2xs text-muted-foreground/40">—</span>
      ),
  },
  {
    key: "description",
    header: "Description",
    cell: (priority) => (
      <span className="text-xs text-muted-foreground">
        <Dash>{priority.description}</Dash>
      </span>
    ),
  },
]

/**
 * `preempts` and `preemptible` are two switches, not one axis: a class can take
 * capacity from others and still be safe from eviction itself, and folding them
 * would make production serving and best-effort batch the same kind of thing.
 */
export function PriorityClassesPage() {
  const list = usePriorityClasses()
  return (
    <div className="flex flex-col">
      <PageHeader title="Priority classes" description="declared · what preempts what" />
      <div className="space-y-4 p-4">
        <DataTable
          columns={columns}
          list={list}
          rowKey={(priority) => priority.id}
          empty="No priority classes."
        />
      </div>
    </div>
  )
}
