import {
  FormDialog,
  FormGrid,
  JsonField,
  LabelsField,
  SelectField,
  TextField,
} from "@/components/layout/form"
import { formatError } from "@/lib/format"
import { CLUSTER_PHASES } from "@/modules/clusters/display"
import { useCreateCluster, useUpdateCluster } from "@/modules/clusters/queries"
import type { Cluster, ClusterPhase } from "@/modules/clusters/types"
import { useNavigate } from "@tanstack/react-router"
import { useState } from "react"
import { toast } from "sonner"

/**
 * Create and edit in one dialog. `name` is only offered on create — the plane
 * has no `name` on its patch body, and a field that silently does nothing is
 * worse than one that is absent.
 */
export function ClusterForm({
  cluster,
  open,
  onOpenChange,
}: {
  cluster?: Cluster
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const editing = !!cluster
  const [name, setName] = useState(cluster?.name ?? "")
  const [displayName, setDisplayName] = useState(cluster?.display_name ?? "")
  const [region, setRegion] = useState(cluster?.region ?? "")
  const [zone, setZone] = useState(cluster?.zone ?? "")
  const [residency, setResidency] = useState(cluster?.residency ?? "")
  const [metricsLabel, setMetricsLabel] = useState(cluster?.metrics_label ?? "")
  const [phase, setPhase] = useState<ClusterPhase>(cluster?.phase ?? "pending")
  const [labels, setLabels] = useState<Record<string, string>>(
    cluster?.labels ?? {}
  )
  const [capabilities, setCapabilities] = useState<unknown>(
    cluster?.capabilities ?? {}
  )
  const [capabilitiesValid, setCapabilitiesValid] = useState(true)

  const navigate = useNavigate()
  const create = useCreateCluster()
  const update = useUpdateCluster()
  const pending = create.isPending || update.isPending

  const submit = () => {
    const shared = {
      display_name: displayName || null,
      region,
      zone: zone || null,
      residency: residency || null,
      phase,
      labels,
      capabilities: (capabilities ?? {}) as Record<string, unknown>,
    }

    if (editing) {
      update.mutate(
        {
          ref: cluster.id,
          body: { ...shared, metrics_label: metricsLabel || null },
          revision: cluster.revision,
        },
        {
          onSuccess: (next) => {
            onOpenChange(false)
            toast.success("Cluster updated", { description: next.name })
          },
          onError: (error) =>
            toast.error(formatError(error, "Could not update cluster")),
        }
      )
      return
    }

    create.mutate(
      { name, ...shared },
      {
        onSuccess: (next) => {
          onOpenChange(false)
          toast.success("Cluster registered", { description: next.id })
          navigate({
            to: "/clusters/$clusterId",
            params: { clusterId: next.id },
          })
        },
        onError: (error) =>
          toast.error(formatError(error, "Could not register cluster")),
      }
    )
  }

  return (
    <FormDialog
      icon="cluster"
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? "Edit cluster" : "Register cluster"}
      description="Declared enrollment. Reachability and sync are observed and cannot be set here."
      error={create.error ?? update.error}
      pending={pending}
      submitLabel={editing ? "Save" : "Register"}
      disabled={
        !capabilitiesValid || region === "" || (!editing && name === "")
      }
      onSubmit={submit}
      wide
    >
      <FormGrid>
        {!editing && (
          <TextField
            label="Name"
            value={name}
            onChange={setName}
            placeholder="eu-west-a1"
            hint="Immutable once declared."
          />
        )}
        <TextField
          label="Display name"
          value={displayName}
          onChange={setDisplayName}
          mono={false}
          placeholder="Frankfurt A1"
        />
        <TextField
          label="Region"
          value={region}
          onChange={setRegion}
          placeholder="eu-central-1"
        />
        <TextField
          label="Zone"
          value={zone}
          onChange={setZone}
          placeholder="a"
        />
        <TextField
          label="Residency"
          value={residency}
          onChange={setResidency}
          placeholder="eu"
        />
        <SelectField
          label="Phase"
          value={phase}
          onChange={(value) => setPhase(value as ClusterPhase)}
          options={CLUSTER_PHASES}
        />
        {editing && (
          <TextField
            label="Metrics label"
            value={metricsLabel}
            onChange={setMetricsLabel}
            placeholder={cluster.name}
            hint="The `cluster` label this cluster's series carry. Empty means no metrics."
          />
        )}
      </FormGrid>

      <LabelsField value={labels} onChange={setLabels} />

      <JsonField
        label="Capabilities"
        value={capabilities}
        onChange={(value, valid) => {
          setCapabilities(value ?? {})
          setCapabilitiesValid(valid)
        }}
        hint="Free-form JSON object."
      />
    </FormDialog>
  )
}
