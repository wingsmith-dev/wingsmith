import {
  FormDialog,
  FormGrid,
  SelectField,
  SwitchField,
  TextField,
} from "@/components/layout/form"
import { formatError } from "@/lib/format"
import { useCreateTenant, useUpdateTenant } from "@/modules/binding/queries"
import type { Tenant, TenantAuth, TenantIngress } from "@/modules/binding/types"
import { useState } from "react"
import { toast } from "sonner"

const INGRESS_OPTIONS: { value: TenantIngress; label: string }[] = [
  { value: "internal", label: "internal: reached only from inside the fleet" },
  { value: "public", label: "public: reached at the tenant's own host" },
]

const AUTH_OPTIONS: { value: TenantAuth; label: string }[] = [
  { value: "none", label: "none: the caller is trusted by listener alone" },
  { value: "api_key", label: "api_key: the gateway checks a key" },
]

/**
 * A tenant carries the billing and isolation root, plus how the gateway is to
 * trust it (docs/decisions/0016): which listener it arrives on, whether a key is
 * checked, and the host that names it.
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
  const [ingress, setIngress] = useState<TenantIngress>(
    tenant?.ingress ?? "internal"
  )
  const [auth, setAuth] = useState<TenantAuth>(tenant?.auth ?? "api_key")
  const [host, setHost] = useState(tenant?.host ?? "")

  const pending = create.isPending || update.isPending
  const error = create.error ?? update.error

  const done = (verb: string) => {
    onOpenChange(false)
    toast.success(`Tenant ${verb}`, { description: slug })
  }

  const submit = () => {
    const hostValue = host.trim() || null
    if (editing) {
      update.mutate(
        {
          ref: tenant.id,
          body: { name, enabled, ingress, auth, host: hostValue },
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
      { slug, name, enabled, ingress, auth, host: hostValue },
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

      <FormGrid>
        <SelectField
          label="Ingress"
          value={ingress}
          onChange={(v) => setIngress(v as TenantIngress)}
          options={INGRESS_OPTIONS}
          hint="Which gateway listener this tenant's traffic arrives on."
        />
        <SelectField
          label="Auth"
          value={auth}
          onChange={(v) => setAuth(v as TenantAuth)}
          options={AUTH_OPTIONS}
          hint="A tenant with a host or public ingress needs api_key; the plane refuses none there unless its operator opted in."
        />
      </FormGrid>

      <TextField
        label="Host"
        value={host}
        onChange={setHost}
        placeholder="globex.hangar.dev"
        hint="The name that resolves to this tenant, matched against the request's Host header."
      />
    </FormDialog>
  )
}
