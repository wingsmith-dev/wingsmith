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
  useUpdateProject,
} from "@/modules/binding/queries"
import type { Project } from "@/modules/binding/types"
import { useState } from "react"
import { toast } from "sonner"

const UNSET = "__none__"

/**
 * The tenant is typed rather than picked: there is no `/v1/tenants` collection
 * to enumerate, so the plane resolves the slug on write.
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
          onError: (e) => toast.error(formatError(e, "Could not update project")),
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
      description="The tenancy unit quota attaches to and X-Hangar-Project names."
      error={error}
      pending={pending}
      disabled={!name || (!editing && (!tenant || !slug))}
      onSubmit={submit}
    >
      {!editing && (
        <FormGrid>
          <TextField
            label="Tenant"
            value={tenant}
            onChange={setTenant}
            placeholder="acme"
            hint="Slug or id of an existing tenant."
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
