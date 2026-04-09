import {
  FormDialog,
  FormGrid,
  SwitchField,
  TextField,
} from "@/components/layout/form"
import { formatError } from "@/lib/format"
import { useCreateTenant, useUpdateTenant } from "@/modules/binding/queries"
import type { Tenant } from "@/modules/binding/types"
import { useState } from "react"
import { toast } from "sonner"

/**
 * A tenant is deliberately three fields. It is the root projects hang off, not a
 * place to keep billing accounts or contacts — those are their own objects when
 * they exist, rather than columns smuggled onto this one.
 */
export function TenantForm({
  open,
  onOpenChange,
  tenant,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  tenant?: Tenant
}) {
  const editing = !!tenant
  const create = useCreateTenant()
  const update = useUpdateTenant()

  const [slug, setSlug] = useState(tenant?.slug ?? "")
  const [name, setName] = useState(tenant?.name ?? "")
  const [enabled, setEnabled] = useState(tenant?.enabled ?? true)

  const pending = create.isPending || update.isPending
  const error = create.error ?? update.error

  const done = (verb: string) => {
    onOpenChange(false)
    toast.success(`Tenant ${verb}`, { description: slug })
  }

  const submit = () => {
    if (editing) {
      update.mutate(
        {
          ref: tenant.id,
          body: { name, enabled },
          revision: tenant.revision,
        },
        {
          onSuccess: () => done("updated"),
          onError: (e) =>
            toast.error(formatError(e, "Could not update tenant")),
        }
      )
      return
    }
    create.mutate(
      { slug, name, enabled },
      {
        onSuccess: () => done("created"),
        onError: (e) => toast.error(formatError(e, "Could not create tenant")),
      }
    )
  }

  return (
    <FormDialog
      icon="tenant"
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? "Edit tenant" : "New tenant"}
      description="The billing and isolation root. Projects hang off it."
      error={error}
      pending={pending}
      disabled={!name || (!editing && !slug)}
      onSubmit={submit}
    >
      <FormGrid>
        {!editing && (
          <TextField
            label="Slug"
            value={slug}
            onChange={setSlug}
            placeholder="acme"
            hint="Identity. It namespaces every project slug beneath it."
          />
        )}
        <TextField label="Name" value={name} onChange={setName} mono={false} />
      </FormGrid>

      <SwitchField
        label="Enabled"
        hint="Disabling one blocks every project beneath it from scheduling."
        checked={enabled}
        onChange={setEnabled}
      />
    </FormDialog>
  )
}
