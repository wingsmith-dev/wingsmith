import {
  FormDialog,
  FormGrid,
  SelectField,
  SwitchField,
  TextField,
} from "@/components/layout/form"
import { formatError } from "@/lib/format"
import {
  useCreateProject,
  usePriorityClassOptions,
  useTenantOptions,
  useUpdateProject,
} from "@/modules/binding/queries"
import type { Project } from "@/modules/binding/types"
import { useState } from "react"
import { toast } from "sonner"

const UNSET = "__none__"

/**
 * The tenant is picked, and the form says so when there is nothing to pick.
 *
 * It used to be a free-text slug resolved on write, from when the API had no
 * tenant collection: on an empty plane every value anyone could type was a
 * `404`, so the first project could not be created and nothing downstream that
 * needs one — promoting a discovered workload, most of all — could be either.
 */
export function ProjectForm({
  open,
  onOpenChange,
  project,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  project?: Project
}) {
  const editing = !!project
  const create = useCreateProject()
  const update = useUpdateProject()
  const { data: priorities } = usePriorityClassOptions()
  const { data: tenants } = useTenantOptions()

  const [tenant, setTenant] = useState(project?.tenant.slug ?? "")
  const [slug, setSlug] = useState(project?.slug ?? "")
  const [name, setName] = useState(project?.name ?? "")
  const [priorityClass, setPriorityClass] = useState(
    project?.default_priority_class ?? UNSET
  )
  const [enabled, setEnabled] = useState(project?.enabled ?? true)

  const pending = create.isPending || update.isPending
  const error = create.error ?? update.error

  const shared = {
    name,
    default_priority_class: priorityClass === UNSET ? null : priorityClass,
    enabled,
  }

  const done = (verb: string) => {
    onOpenChange(false)
    toast.success(`Project ${verb}`, { description: slug })
  }

  const submit = () => {
    if (editing) {
      update.mutate(
        { ref: project.id, body: shared, revision: project.revision },
        {
          onSuccess: () => done("updated"),
          onError: (e) =>
            toast.error(formatError(e, "Could not update project")),
        }
      )
      return
    }
    create.mutate(
      { tenant, slug, ...shared },
      {
        onSuccess: () => done("created"),
        onError: (e) => toast.error(formatError(e, "Could not create project")),
      }
    )
  }

  return (
    <FormDialog
      icon="project"
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? "Edit project" : "New project"}
      description="The unit quota and priority attach to, beneath a tenant."
      error={error}
      pending={pending}
      disabled={!name || (!editing && (!tenant || !slug))}
      onSubmit={submit}
    >
      {!editing && tenants?.length === 0 && (
        <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3">
          <p className="font-mono text-2xs text-amber-600 dark:text-amber-400">
            there are no tenants yet — a project hangs off one, so make a tenant
            first
          </p>
        </div>
      )}

      {!editing && (
        <FormGrid>
          <SelectField
            label="Tenant"
            value={tenant}
            onChange={setTenant}
            hint="The billing root this project hangs off."
            options={(tenants ?? []).map((t) => ({
              value: t.slug,
              label: t.name,
            }))}
          />
          <TextField
            label="Slug"
            value={slug}
            onChange={setSlug}
            placeholder="acme-serving"
            hint="Identity. It cannot be changed later."
          />
        </FormGrid>
      )}

      <TextField label="Name" value={name} onChange={setName} mono={false} />

      <SelectField
        label="Default priority class"
        value={priorityClass}
        onChange={setPriorityClass}
        options={[
          { value: UNSET, label: "none" },
          ...(priorities ?? []).map((p) => ({ value: p.name, label: p.name })),
        ]}
      />

      <SwitchField
        label="Enabled"
        hint="A project inside a disabled tenant is still blocked."
        checked={enabled}
        onChange={setEnabled}
      />
    </FormDialog>
  )
}
