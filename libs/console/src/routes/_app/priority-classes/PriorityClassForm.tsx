import {
  FormDialog,
  FormGrid,
  NumberField,
  SwitchField,
  TextField,
} from "@/components/layout/form"
import { formatError } from "@/lib/format"
import {
  useCreatePriorityClass,
  useUpdatePriorityClass,
} from "@/modules/binding/queries"
import type { PriorityClass } from "@/modules/binding/types"
import { useState } from "react"
import { toast } from "sonner"

/**
 * The name is fixed at creation: workloads reference a class by string, and
 * renaming one would silently repoint everything that names it.
 */
export function PriorityClassForm({
  open,
  onOpenChange,
  priorityClass,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  priorityClass?: PriorityClass
}) {
  const editing = !!priorityClass
  const create = useCreatePriorityClass()
  const update = useUpdatePriorityClass()

  const [name, setName] = useState(priorityClass?.name ?? "")
  const [value, setValue] = useState<number | null>(priorityClass?.value ?? 500)
  const [preemptible, setPreemptible] = useState(
    priorityClass?.preemptible ?? true
  )
  const [preempts, setPreempts] = useState(priorityClass?.preempts ?? false)
  const [isDefault, setIsDefault] = useState(priorityClass?.is_default ?? false)
  const [description, setDescription] = useState(
    priorityClass?.description ?? ""
  )

  const pending = create.isPending || update.isPending
  const error = create.error ?? update.error

  const shared = {
    value: value ?? 0,
    preemptible,
    preempts,
    is_default: isDefault,
    description: description || null,
  }

  const done = (verb: string) => {
    onOpenChange(false)
    toast.success(`Priority class ${verb}`, { description: name })
  }

  const submit = () => {
    if (editing) {
      update.mutate(
        {
          ref: priorityClass.id,
          body: shared,
          revision: priorityClass.revision,
        },
        {
          onSuccess: () => done("updated"),
          onError: (e) => toast.error(formatError(e, "Could not update class")),
        }
      )
      return
    }
    create.mutate(
      { name, ...shared },
      {
        onSuccess: () => done("created"),
        onError: (e) => toast.error(formatError(e, "Could not create class")),
      }
    )
  }

  return (
    <FormDialog
      icon="priority-class"
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? "Edit priority class" : "New priority class"}
      description="What preempts what. The two switches are independent."
      error={error}
      pending={pending}
      disabled={(!editing && !name) || value === null}
      onSubmit={submit}
    >
      <FormGrid>
        {!editing && (
          <TextField
            label="Name"
            value={name}
            onChange={setName}
            placeholder="interactive"
            hint="Identity. Workloads reference it by this string."
          />
        )}
        <NumberField
          label="Value"
          value={value}
          onChange={setValue}
          hint="Higher wins."
        />
      </FormGrid>

      <TextField
        label="Description"
        value={description}
        onChange={setDescription}
        mono={false}
      />

      <SwitchField
        label="Preempts others"
        hint="May take capacity from lower classes."
        checked={preempts}
        onChange={setPreempts}
      />
      <SwitchField
        label="Can be preempted"
        hint="May lose capacity to higher classes."
        checked={preemptible}
        onChange={setPreemptible}
      />
      <SwitchField
        label="Default"
        hint="Used by workloads that name no class."
        checked={isDefault}
        onChange={setIsDefault}
      />
    </FormDialog>
  )
}
