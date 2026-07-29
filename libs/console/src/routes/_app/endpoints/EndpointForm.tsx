import {
  FailoverField,
  TargetsField,
} from "@/components/demand/endpoint-targets"
import {
  FormDialog,
  FormGrid,
  LabelsField,
  ListField,
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
  const [aliases, setAliases] = useState<string[]>(endpoint?.aliases ?? [])
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
    aliases,
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
      {/* The name is shown when editing and cannot be changed there. The plane
          refuses it — the name is the handle in URLs, in metrics labels and in
          every link the console holds, and an endpoint exists so that what is
          behind it can be replaced without anybody noticing. `slug` is the
          public model id and is patchable, which is the point of the split.
          Hiding the field entirely made that read as an omission. */}
      <FormGrid>
        <TextField
          label="Name"
          value={name}
          onChange={setName}
          disabled={editing}
          hint={
            editing
              ? "Fixed after creation. Change `slug` to move the public model id."
              : undefined
          }
        />
        {!editing && (
          <SelectField
            label="Project"
            value={project}
            onChange={setProject}
            options={(projects ?? []).map((p) => ({
              value: p.id,
              label: p.slug,
            }))}
          />
        )}
      </FormGrid>

      <FormGrid>
        <TextField
          label="Slug"
          value={slug}
          onChange={setSlug}
          placeholder="defaults to the model behind the first target"
          hint="What clients send as `model`. Blank defaults to the first target's model."
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

      {/* Beside the slug and not under a disclosure, because an alias is the
          other half of the same decision: the slug is the id clients should
          send and these are the ids that still work. Retiring one is deleting
          its line, and the plane reads an empty set as "retire them all" — the
          field is only sent because the form always sends the whole shape. */}
      <ListField
        label="Aliases"
        value={aliases}
        onChange={setAliases}
        placeholder="moonshotai/kimi-k3"
        hint="One per line. Other ids clients may send as `model`; they resolve to the slug and are never listed by /v1/models."
      />

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
