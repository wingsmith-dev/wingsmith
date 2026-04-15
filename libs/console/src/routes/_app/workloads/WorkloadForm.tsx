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
import {
  usePriorityClassOptions,
  useProjectOptions,
} from "@/modules/binding/queries"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import {
  WORKLOAD_KINDS,
  WORKLOAD_STRATEGIES,
  scaleUnit,
  splittable,
} from "@/modules/demand/display"
import {
  useCreateWorkload,
  useRecipeOptions,
  useUpdateWorkload,
} from "@/modules/demand/queries"
import type { Workload } from "@/modules/demand/types"
import { useState } from "react"
import { toast } from "sonner"

/**
 * What to run, and how much of it — never where.
 *
 * There is no cluster to choose here, because choosing one is placement's job:
 * the form declares a total and the clusters the workload may use, and the
 * per-cluster table on the detail page is the answer. The one exception is the
 * pin, which is offered last and labelled as what it is — an override that
 * takes the decision away from the scheduler and forces a single cluster.
 *
 * Recipe, project and kind are create-only. A workload is recipe × kind the way
 * a deployment is recipe × cluster; changing either is a new workload, and the
 * endpoint in front of it is the stable name that makes that cheap.
 *
 * Scale is create-only too. Changing it afterwards is `workload.scale`, an
 * operation with a phase, because it can sit pending on quota indefinitely.
 */
export function WorkloadForm({
  open,
  onOpenChange,
  workload,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  workload?: Workload
}) {
  const editing = !!workload
  const create = useCreateWorkload()
  const update = useUpdateWorkload()
  const { data: clusters } = useClusterOptions()
  const { data: recipes } = useRecipeOptions()
  const { data: projects } = useProjectOptions()
  const { data: priorities } = usePriorityClassOptions()

  const [recipe, setRecipe] = useState(workload?.recipe_id ?? "")
  const [project, setProject] = useState(workload?.project_id ?? "")
  const [name, setName] = useState(workload?.name ?? "")
  const [kind, setKind] = useState(workload?.kind ?? "service")
  const [priorityClass, setPriorityClass] = useState(
    workload?.priority_class ?? ""
  )
  const [minScale, setMinScale] = useState<number | null>(
    workload?.scale.min ?? 1
  )
  const [maxScale, setMaxScale] = useState<number | null>(
    workload?.scale.max ?? 1
  )
  const [desiredScale, setDesiredScale] = useState<number | null>(
    workload?.scale.desired ?? 1
  )
  const [strategy, setStrategy] = useState(workload?.strategy ?? "pack")
  const [maxClusters, setMaxClusters] = useState<number | null>(
    workload?.max_clusters ?? null
  )
  const [pinned, setPinned] = useState("")
  const [gangSize, setGangSize] = useState<number | null>(
    workload?.gang_size ?? 1
  )
  const [preemptible, setPreemptible] = useState(workload?.preemptible ?? false)
  const [deadline, setDeadline] = useState(workload?.deadline_at ?? "")
  const [overrides, setOverrides] = useState<unknown>(workload?.overrides ?? {})
  const [overridesValid, setOverridesValid] = useState(true)
  const [labels, setLabels] = useState<Record<string, string>>(
    workload?.labels ?? {}
  )

  const pending = create.isPending || update.isPending
  const error = create.error ?? update.error
  const unit = scaleUnit(kind)
  const fansOut = splittable(kind) && !pinned

  const done = (verb: string) => {
    onOpenChange(false)
    toast.success(`Workload ${verb}`, { description: name })
  }

  const submit = () => {
    if (editing) {
      update.mutate(
        {
          ref: workload.id,
          body: {
            name,
            priority_class: priorityClass || undefined,
            preemptible,
            deadline_at: deadline || null,
            strategy,
            max_clusters: maxClusters,
            overrides: (overrides as Record<string, unknown>) ?? {},
            labels,
          },
          revision: workload.revision,
        },
        {
          onSuccess: () => done("updated"),
          onError: (e) =>
            toast.error(formatError(e, "Could not update workload")),
        }
      )
      return
    }
    // A gang does not have a range: its ranks are co-scheduled, so the three
    // totals are its gang size and placement rejects anything else.
    const total = kind === "gang" ? (gangSize ?? 1) : (desiredScale ?? 1)
    create.mutate(
      {
        recipe,
        name,
        project,
        kind,
        cluster: pinned || null,
        priority_class: priorityClass || null,
        min_scale: kind === "gang" ? total : (minScale ?? 1),
        max_scale: kind === "gang" ? total : (maxScale ?? 1),
        desired_scale: total,
        strategy,
        max_clusters: maxClusters,
        gang_size: gangSize ?? 1,
        preemptible,
        deadline_at: deadline || null,
        overrides: (overrides as Record<string, unknown>) ?? {},
        labels,
      },
      {
        onSuccess: () => done("created"),
        onError: (e) => toast.error(formatError(e, "Could not create workload")),
      }
    )
  }

  return (
    <FormDialog
      icon="workload"
      wide
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? "Edit workload" : "New workload"}
      description="A recipe, a total, and the clusters it may use. Placement decides which ones."
      error={error}
      pending={pending}
      disabled={!name || (!editing && (!recipe || !project)) || !overridesValid}
      onSubmit={submit}
    >
      {!editing && (
        <FormGrid>
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
            options={(projects ?? []).map((p) => ({
              value: p.id,
              label: p.slug,
            }))}
          />
          <SelectField
            label="Kind"
            value={kind}
            onChange={setKind}
            options={WORKLOAD_KINDS}
          />
          {kind === "gang" && (
            <NumberField
              label="Gang size (ranks)"
              value={gangSize}
              onChange={setGangSize}
              min={1}
              hint="Co-scheduled on one cluster. A gang is never split."
            />
          )}
        </FormGrid>
      )}

      <FormGrid>
        <TextField label="Name" value={name} onChange={setName} />
        <SelectField
          label="Priority class"
          value={priorityClass}
          onChange={setPriorityClass}
          options={(priorities ?? []).map((p) => ({
            value: p.name,
            label: p.name,
          }))}
        />
        {!editing && kind !== "gang" && (
          <>
            <NumberField
              label={`Desired ${unit}`}
              value={desiredScale}
              onChange={setDesiredScale}
              min={0}
              hint="The total across every cluster, not a per-cluster number."
            />
            <NumberField
              label={`Minimum ${unit}`}
              value={minScale}
              onChange={setMinScale}
              min={0}
            />
            <NumberField
              label={`Maximum ${unit}`}
              value={maxScale}
              onChange={setMaxScale}
              min={0}
            />
          </>
        )}
        <TextField
          label="Deadline"
          value={deadline}
          onChange={setDeadline}
          placeholder="2026-01-01T00:00:00Z"
          hint="RFC 3339, or blank for none."
        />
      </FormGrid>

      {fansOut && (
        <FormGrid>
          <SelectField
            label="Strategy"
            value={strategy}
            onChange={setStrategy}
            options={WORKLOAD_STRATEGIES}
          />
          <NumberField
            label="Cluster fan-out cap"
            value={maxClusters}
            onChange={setMaxClusters}
            min={1}
            hint="Blank for no cap. Pack fills a cluster before reaching for the next."
          />
        </FormGrid>
      )}

      {!editing && (
        <SelectField
          label="Pin to cluster"
          value={pinned}
          onChange={setPinned}
          options={(clusters ?? []).map((c) => ({
            value: c.id,
            label: clusterLabel(c),
          }))}
          hint="An override, not the normal path: a pinned workload gets exactly one deployment and is never checked for capacity."
        />
      )}

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
        hint="Departures from the recipe, applied on every cluster this runs on."
        onChange={(value, valid) => {
          setOverrides(value ?? {})
          setOverridesValid(valid)
        }}
      />

      <LabelsField value={labels} onChange={setLabels} />
    </FormDialog>
  )
}
