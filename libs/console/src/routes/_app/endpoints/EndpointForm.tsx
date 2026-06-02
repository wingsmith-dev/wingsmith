import {
  FailoverField,
  TargetsField,
} from "@/components/demand/endpoint-targets"
import {
  FormDialog,
  FormGrid,
  LabelsField,
  SelectField,
  SwitchField,
  TextField,
} from "@/components/layout/form"
import { formatError } from "@/lib/format"
import { useProjectOptions } from "@/modules/binding/queries"
import {
  useCreateEndpoint,
  useDeploymentOptions,
  useUpdateEndpoint,
} from "@/modules/demand/queries"
import type { Endpoint, EndpointTargetRequest } from "@/modules/demand/types"
import { useState } from "react"
import { toast } from "sonner"

const VISIBILITIES = ["private", "public"]
const ROUTING_STRATEGIES = ["weighted", "failover", "latency"]

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
  const { data: deployments } = useDeploymentOptions()

  const [name, setName] = useState(endpoint?.name ?? "")
  const [project, setProject] = useState(endpoint?.project_id ?? "")
  const [slug, setSlug] = useState(endpoint?.slug ?? "")
  const [visibility, setVisibility] = useState(
    endpoint?.visibility ?? "private"
  )
  const [routing, setRouting] = useState(
    endpoint?.routing_strategy ?? "weighted"
  )
  const [enabled, setEnabled] = useState(endpoint?.enabled ?? true)
  const [targets, setTargets] = useState<EndpointTargetRequest[]>(
    endpoint?.targets.map((t) => ({
      deployment: t.deployment_id,
      weight: t.weight,
      role: t.role,
      enabled: t.enabled,
      served_model_name: t.served_model_name,
    })) ?? []
  )
  const [failover, setFailover] = useState<string[]>(
    endpoint?.failover_order ?? []
  )
  const [labels, setLabels] = useState<Record<string, string>>(
    endpoint?.labels ?? {}
  )

  const pending = create.isPending || update.isPending
  const error = create.error ?? update.error

  const shared = {
    slug: slug.trim(),
    visibility,
    routing_strategy: routing,
    enabled,
    targets,
    failover_order: failover,
    labels,
  }

  /** A row with no deployment chosen yet is a row the plane would reject. */
  const complete =
    targets.every((t) => !!t.deployment) && failover.every((id) => !!id)

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
          onError: (e) =>
            toast.error(formatError(e, "Could not update endpoint")),
        }
      )
      return
    }
    create.mutate(
      { name, project, ...shared },
      {
        onSuccess: () => done("published"),
        onError: (e) =>
          toast.error(formatError(e, "Could not create endpoint")),
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
      disabled={(!editing && (!name || !project)) || !complete}
      onSubmit={submit}
    >
      {!editing && (
        <FormGrid>
          <TextField label="Name" value={name} onChange={setName} />
          <SelectField
            label="Project"
            value={project}
            onChange={setProject}
            options={(projects ?? []).map((p) => ({
              value: p.id,
              label: p.slug,
            }))}
          />
        </FormGrid>
      )}

      <FormGrid>
        <TextField
          label="Slug"
          value={slug}
          onChange={setSlug}
          placeholder="defaults to the model behind the first target"
          hint="What clients send as `model`. Blank takes the slug of the weights behind the first target, which is what you want unless you are publishing an alias. The engine answers to the artifact's slug either way — the gateway rewrites between them on its next poll, so changing this restarts nothing."
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

      <TargetsField
        value={targets}
        deployments={deployments ?? []}
        onChange={setTargets}
      />

      <FailoverField
        value={failover}
        deployments={deployments ?? []}
        onChange={setFailover}
      />

      <LabelsField value={labels} onChange={setLabels} />
    </FormDialog>
  )
}
