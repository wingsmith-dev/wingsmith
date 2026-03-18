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
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { useCreatePool, useUpdatePool } from "@/modules/supply/queries"
import type { Pool } from "@/modules/supply/types"
import { useState } from "react"
import { toast } from "sonner"

/**
 * Create and edit in one dialog. Cluster and name are create-only: a pool
 * cannot be moved between clusters, and the patch body has neither field.
 */
export function PoolForm({
  pool,
  defaultCluster,
  open,
  onOpenChange,
}: {
  pool?: Pool
  defaultCluster?: string
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const editing = !!pool
  const { data: clusters } = useClusterOptions()
  const [cluster, setCluster] = useState(defaultCluster ?? "")
  const [name, setName] = useState(pool?.name ?? "")
  const [deviceClass, setDeviceClass] = useState(pool?.device_class ?? "")
  const [devicesPerNode, setDevicesPerNode] = useState<number | null>(
    pool?.devices_per_node ?? 8
  )
  const [minNodes, setMinNodes] = useState<number | null>(pool?.min_nodes ?? 0)
  const [maxNodes, setMaxNodes] = useState<number | null>(pool?.max_nodes ?? 0)
  const [enabled, setEnabled] = useState(pool?.enabled ?? true)
  const [labels, setLabels] = useState<Record<string, string>>(pool?.labels ?? {})
  const [nodeShape, setNodeShape] = useState<unknown>(pool?.node_shape ?? {})
  const [shapeValid, setShapeValid] = useState(true)

  const create = useCreatePool()
  const update = useUpdatePool()
  const pending = create.isPending || update.isPending

  const bounded = (minNodes ?? 0) <= (maxNodes ?? 0)

  const submit = () => {
    const shared = {
      device_class: deviceClass,
      devices_per_node: devicesPerNode ?? 0,
      node_shape: (nodeShape ?? {}) as Record<string, unknown>,
      min_nodes: minNodes ?? 0,
      max_nodes: maxNodes ?? 0,
      labels,
      enabled,
    }

    if (editing) {
      update.mutate(
        { ref: pool.id, body: shared, revision: pool.revision },
        {
          onSuccess: (next) => {
            onOpenChange(false)
            toast.success("Pool updated", { description: next.name })
          },
          onError: (error) => toast.error(formatError(error, "Could not update pool")),
        }
      )
      return
    }

    create.mutate(
      { cluster, name, ...shared },
      {
        onSuccess: (next) => {
          onOpenChange(false)
          toast.success("Pool declared", { description: next.id })
        },
        onError: (error) => toast.error(formatError(error, "Could not create pool")),
      }
    )
  }

  return (
    <FormDialog
      icon="pool"
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? "Edit pool" : "New pool"}
      description="Declared provisioning spec. Membership is observed and follows on its own."
      error={create.error ?? update.error}
      pending={pending}
      submitLabel={editing ? "Save" : "Create pool"}
      disabled={
        !shapeValid ||
        !bounded ||
        deviceClass === "" ||
        (!editing && (cluster === "" || name === ""))
      }
      onSubmit={submit}
      wide
    >
      <FormGrid>
        {!editing && (
          <>
            <SelectField
              label="Cluster"
              value={cluster}
              onChange={setCluster}
              options={(clusters ?? []).map((entry) => ({
                value: entry.id,
                label: clusterLabel(entry),
              }))}
              placeholder="Select a cluster…"
            />
            <TextField
              label="Name"
              value={name}
              onChange={setName}
              placeholder="h100-sxm-8x"
            />
          </>
        )}
        <TextField
          label="Device class"
          value={deviceClass}
          onChange={setDeviceClass}
          placeholder="h100-80gb-sxm"
        />
        <NumberField
          label="Devices per node"
          value={devicesPerNode}
          onChange={setDevicesPerNode}
          min={0}
        />
        <NumberField
          label="Min nodes"
          value={minNodes}
          onChange={setMinNodes}
          min={0}
        />
        <NumberField
          label="Max nodes"
          value={maxNodes}
          onChange={setMaxNodes}
          min={0}
          hint={bounded ? undefined : "Max must not be below min."}
        />
      </FormGrid>

      <SwitchField
        label="Enabled"
        hint="A disabled pool stays declared but is not provisioned against."
        checked={enabled}
        onChange={setEnabled}
      />

      <LabelsField value={labels} onChange={setLabels} />

      <JsonField
        label="Node shape"
        value={nodeShape}
        onChange={(value, valid) => {
          setNodeShape(value ?? {})
          setShapeValid(valid)
        }}
        hint={'Free-form JSON object, e.g. {"sku": "hgx-h100"}.'}
      />
    </FormDialog>
  )
}
