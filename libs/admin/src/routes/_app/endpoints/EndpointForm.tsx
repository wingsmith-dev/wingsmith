import {
  FormDialog,
  FormGrid,
  JsonField,
  LabelsField,
  SelectField,
  SwitchField,
  TextField,
} from "@/components/layout/form"
import { formatError } from "@/lib/format"
import { useProjectOptions } from "@/modules/binding/queries"
import { useCreateEndpoint, useUpdateEndpoint } from "@/modules/demand/queries"
import type { Endpoint, EndpointTargetRequest } from "@/modules/demand/types"
import { useState } from "react"
import { toast } from "sonner"

const VISIBILITIES = ["private", "public"]
const ROUTING_STRATEGIES = ["weighted", "failover", "latency"]

/**
 * Targets are edited as JSON rather than a row builder: the set is weighted and
 * ordered together, and hiding that behind per-row controls makes a rollout
 * look like several independent edits when it is one.
 */
export function EndpointForm({
  open,
  onOpenChange,
  endpoint,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  endpoint?: Endpoint
}) {
  const editing = !!endpoint
  const create = useCreateEndpoint()
  const update = useUpdateEndpoint()
  const { data: projects } = useProjectOptions()

  const [name, setName] = useState(endpoint?.name ?? "")
  const [project, setProject] = useState(endpoint?.project_id ?? "")
  const [displayName, setDisplayName] = useState(endpoint?.display_name ?? "")
  const [visibility, setVisibility] = useState(endpoint?.visibility ?? "private")
  const [routing, setRouting] = useState(endpoint?.routing_strategy ?? "weighted")
  const [enabled, setEnabled] = useState(endpoint?.enabled ?? true)
  const [targets, setTargets] = useState<unknown>(
    endpoint?.targets.map((t) => ({
      deployment: t.deployment_id,
      weight: t.weight,
      role: t.role,
      enabled: t.enabled,
    })) ?? []
  )
  const [targetsValid, setTargetsValid] = useState(true)
  const [failover, setFailover] = useState<unknown>(endpoint?.failover_order ?? [])
  const [failoverValid, setFailoverValid] = useState(true)
  const [labels, setLabels] = useState<Record<string, string>>(endpoint?.labels ?? {})

  const pending = create.isPending || update.isPending
  const error = create.error ?? update.error

  const shared = {
    display_name: displayName || null,
    visibility,
    routing_strategy: routing,
    enabled,
    targets: (targets as EndpointTargetRequest[]) ?? [],
    failover_order: (failover as string[]) ?? [],
    labels,
  }

  const done = (verb: string) => {
    onOpenChange(false)
    toast.success(`Endpoint ${verb}`, { description: name })
  }

  const submit = () => {
    if (editing) {
      update.mutate(
        { ref: endpoint.id, body: shared, revision: endpoint.revision },
        {
          onSuccess: () => done("updated"),
          onError: (e) => toast.error(formatError(e, "Could not update endpoint")),
        }
      )
      return
    }
    create.mutate(
      { name, project, ...shared },
      {
        onSuccess: () => done("published"),
        onError: (e) => toast.error(formatError(e, "Could not create endpoint")),
      }
    )
  }

  return (
    <FormDialog
      icon="endpoint"
      wide
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? "Edit endpoint" : "New endpoint"}
      description="The stable public name clients call. It spans clusters by construction."
      error={error}
      pending={pending}
      submitLabel={editing ? "Save" : "Publish"}
      disabled={
        (!editing && (!name || !project)) || !targetsValid || !failoverValid
      }
      onSubmit={submit}
    >
      {!editing && (
        <FormGrid>
          <TextField label="Name" value={name} onChange={setName} />
          <SelectField
            label="Project"
            value={project}
            onChange={setProject}
            options={(projects ?? []).map((p) => ({ value: p.id, label: p.slug }))}
          />
        </FormGrid>
      )}

      <FormGrid>
        <TextField
          label="Display name"
          value={displayName}
          onChange={setDisplayName}
          mono={false}
        />
        <SelectField
          label="Visibility"
          value={visibility}
          onChange={setVisibility}
          options={VISIBILITIES}
        />
        <SelectField
          label="Routing strategy"
          value={routing}
          onChange={setRouting}
          options={ROUTING_STRATEGIES}
        />
      </FormGrid>

      <SwitchField label="Enabled" checked={enabled} onChange={setEnabled} />

      <JsonField
        label="Targets"
        value={targets}
        rows={6}
        hint="Array of {deployment, weight, role, enabled}."
        onChange={(value, valid) => {
          setTargets(value ?? [])
          setTargetsValid(valid)
        }}
      />

      <JsonField
        label="Failover order"
        value={failover}
        rows={3}
        hint="Array of deployment ids, most preferred first."
        onChange={(value, valid) => {
          setFailover(value ?? [])
          setFailoverValid(valid)
        }}
      />

      <LabelsField value={labels} onChange={setLabels} />
    </FormDialog>
  )
}
