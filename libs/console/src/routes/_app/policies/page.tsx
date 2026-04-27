import { CardGrid } from "@/components/binding/card-grid"
import { DeleteDialog } from "@/components/layout/destructive"
import { PageHeader } from "@/components/layout/page-header"
import { Button } from "@/components/ui/button"
import { formatError } from "@/lib/format"
import { useDeletePolicy, usePolicies } from "@/modules/binding/queries"
import type { Policy } from "@/modules/binding/types"
import { PolicyCard } from "./PolicyCard"
import { PolicyForm } from "./PolicyForm"
import { useState } from "react"
import { toast } from "sonner"

/**
 * Cards rather than a table. A policy is five lists and an attachment set, and
 * a row that flattens them either truncates the lists or makes every other
 * column unreadable.
 */
export function PoliciesPage() {
  const list = usePolicies()
  const remove = useDeletePolicy()
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Policy | undefined>()
  const [doomed, setDoomed] = useState<Policy | undefined>()

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Policies"
        description="declared · placement constraints and what they bind to"
        actions={<Button onClick={() => setCreating(true)}>New policy</Button>}
      />
      <div className="space-y-4 p-4">
        <CardGrid
          list={list}
          rowKey={(policy) => policy.id}
          empty="No policies declared."
        >
          {(policy) => (
            <PolicyCard
              policy={policy}
              onEdit={() => setEditing(policy)}
              onDelete={() => setDoomed(policy)}
            />
          )}
        </CardGrid>
      </div>

      {creating && (
        <PolicyForm open onOpenChange={(open) => !open && setCreating(false)} />
      )}
      {editing && (
        <PolicyForm
          key={editing.revision}
          open
          policy={editing}
          onOpenChange={(open) => !open && setEditing(undefined)}
        />
      )}
      <DeleteDialog
        icon="policy"
        open={!!doomed}
        onOpenChange={(open) => !open && setDoomed(undefined)}
        kind="policy"
        name={doomed?.name ?? ""}
        note="Everything it was attached to becomes unconstrained."
        error={remove.error}
        pending={remove.isPending}
        onConfirm={() =>
          doomed &&
          remove.mutate(doomed.id, {
            onSuccess: () => {
              setDoomed(undefined)
              toast.success("Policy deleted", { description: doomed.name })
            },
            onError: (e) =>
              toast.error(formatError(e, "Could not delete policy")),
          })
        }
      />
    </div>
  )
}
