import {
  FormDialog,
  FormGrid,
  JsonField,
  LabelsField,
  NumberField,
  SelectField,
  SwitchField,
  TextField,
} from "@/components/layout/form"
import { formatError } from "@/lib/format"
import { usePriorityClassOptions, useProjectOptions } from "@/modules/binding/queries"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { WORKLOAD_TYPES } from "@/modules/demand/display"
import {
  useCreateDeployment,
  useRecipeOptions,
  useUpdateDeployment,
} from "@/modules/demand/queries"
import type { Deployment } from "@/modules/demand/types"
import { useState } from "react"
import { toast } from "sonner"

/**
 * Cluster, recipe, project and workload type are only offered on create. The
 * plane placed this deployment on that cluster; moving it is a new deployment,
 * not a patch, and the API has no field for it.
 */
export function DeploymentForm({
  open,
  onOpenChange,
  deployment,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  deployment?: Deployment
}) {
  const editing = !!deployment
  const create = useCreateDeployment()
  const update = useUpdateDeployment()
  const { data: clusters } = useClusterOptions()
  const { data: recipes } = useRecipeOptions()
  const { data: projects } = useProjectOptions()
  const { data: priorities } = usePriorityClassOptions()

  const [cluster, setCluster] = useState(deployment?.cluster_id ?? "")
  const [recipe, setRecipe] = useState(deployment?.recipe_id ?? "")
  const [project, setProject] = useState(deployment?.workload.project_id ?? "")
  const [name, setName] = useState(deployment?.name ?? "")
  const [type, setType] = useState(deployment?.workload.type ?? "service")
  const [priorityClass, setPriorityClass] = useState(
    deployment?.workload.priority_class ?? ""
  )
  const [minReplicas, setMinReplicas] = useState<number | null>(
    deployment?.min_replicas ?? 1
  )
  const [maxReplicas, setMaxReplicas] = useState<number | null>(
    deployment?.max_replicas ?? 1
  )
  const [desiredReplicas, setDesiredReplicas] = useState<number | null>(
    deployment?.desired_replicas ?? 1
  )
  const [gangSize, setGangSize] = useState<number | null>(
    deployment?.workload.gang_size ?? 1
  )
  const [enabled, setEnabled] = useState(deployment?.enabled ?? true)
  const [preemptible, setPreemptible] = useState(
    deployment?.workload.preemptible ?? false
  )
  const [deadline, setDeadline] = useState(deployment?.workload.deadline_at ?? "")
  const [overrides, setOverrides] = useState<unknown>(deployment?.overrides ?? {})
  const [overridesValid, setOverridesValid] = useState(true)
  const [labels, setLabels] = useState<Record<string, string>>(deployment?.labels ?? {})

  const pending = create.isPending || update.isPending
  const error = create.error ?? update.error

  const shared = {
    name,
    min_replicas: minReplicas ?? 1,
    max_replicas: maxReplicas ?? 1,
    desired_replicas: desiredReplicas ?? 1,
    enabled,
    preemptible,
    deadline_at: deadline || null,
    overrides: (overrides as Record<string, unknown>) ?? {},
    labels,
  }

  const done = (verb: string) => {
    onOpenChange(false)
    toast.success(`Deployment ${verb}`, { description: name })
  }

  const submit = () => {
    if (editing) {
      update.mutate(
        {
          ref: deployment.id,
          body: { ...shared, priority_class: priorityClass || undefined },
          revision: deployment.revision,
        },
        {
          onSuccess: () => done("updated"),
          onError: (e) => toast.error(formatError(e, "Could not update deployment")),
        }
      )
      return
    }
    create.mutate(
      {
        cluster,
        recipe,
        project,
        type,
        gang_size: gangSize ?? 1,
        priority_class: priorityClass || null,
        ...shared,
      },
      {
        onSuccess: () => done("created"),
        onError: (e) => toast.error(formatError(e, "Could not create deployment")),
      }
    )
  }

  return (
    <FormDialog
      icon="deployment"
      wide
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? "Edit deployment" : "New deployment"}
      description="A recipe on one cluster. Placement decides where; this is what we asked for."
      error={error}
      pending={pending}
      disabled={
        !name ||
        (!editing && (!cluster || !recipe || !project)) ||
        !overridesValid
      }
      onSubmit={submit}
    >
      {!editing && (
        <FormGrid>
          <SelectField
            label="Cluster"
            value={cluster}
            onChange={setCluster}
            options={(clusters ?? []).map((c) => ({
              value: c.id,
              label: clusterLabel(c),
            }))}
          />
          <SelectField
            label="Recipe"
            value={recipe}
            onChange={setRecipe}
            options={(recipes ?? []).map((r) => ({
              value: r.id,
              label: `${r.name} v${r.version}`,
            }))}
          />
          <SelectField
            label="Project"
            value={project}
            onChange={setProject}
            options={(projects ?? []).map((p) => ({ value: p.id, label: p.slug }))}
          />
          <SelectField
            label="Workload type"
            value={type}
            onChange={setType}
            options={WORKLOAD_TYPES}
          />
          <NumberField
            label="Gang size"
            value={gangSize}
            onChange={setGangSize}
            min={1}
          />
        </FormGrid>
      )}

      <FormGrid>
        <TextField label="Name" value={name} onChange={setName} />
        <SelectField
          label="Priority class"
          value={priorityClass}
          onChange={setPriorityClass}
          options={(priorities ?? []).map((p) => ({ value: p.name, label: p.name }))}
        />
        <NumberField
          label="Min replicas"
          value={minReplicas}
          onChange={setMinReplicas}
          min={0}
        />
        <NumberField
          label="Max replicas"
          value={maxReplicas}
          onChange={setMaxReplicas}
          min={0}
        />
        <NumberField
          label="Desired replicas"
          value={desiredReplicas}
          onChange={setDesiredReplicas}
          min={0}
        />
        <TextField
          label="Deadline"
          value={deadline}
          onChange={setDeadline}
          placeholder="2026-01-01T00:00:00Z"
          hint="RFC 3339, or blank for none."
        />
      </FormGrid>

      <SwitchField label="Enabled" checked={enabled} onChange={setEnabled} />
      <SwitchField
        label="Preemptible"
        hint="Whether higher-priority work may take this capacity back."
        checked={preemptible}
        onChange={setPreemptible}
      />

      <JsonField
        label="Overrides"
        value={overrides}
        rows={4}
        hint="Per-deployment departures from the recipe."
        onChange={(value, valid) => {
          setOverrides(value ?? {})
          setOverridesValid(valid)
        }}
      />

      <LabelsField value={labels} onChange={setLabels} />
    </FormDialog>
  )
}
