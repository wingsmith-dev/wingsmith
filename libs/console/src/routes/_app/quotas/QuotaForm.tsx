import { DeviceClassField } from "@/components/supply/device-class"
import {
  FormDialog,
  FormGrid,
  NumberField,
  SelectField,
  SwitchField,
} from "@/components/layout/form"
import { formatError } from "@/lib/format"
import {
  useCreateQuota,
  useProjectOptions,
  useUpdateQuota,
} from "@/modules/binding/queries"
import type { Quota } from "@/modules/binding/types"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { useState } from "react"
import { toast } from "sonner"

const EVERY_CLUSTER = "__all__"

/**
 * Project, cluster and device class are what the quota is *about* and are only
 * offered on create: changing one would silently move the guarantee to a
 * different pool of capacity, not adjust this one.
 */
export function QuotaForm({
  open,
  onOpenChange,
  quota,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  quota?: Quota
}) {
  const editing = !!quota
  const create = useCreateQuota()
  const update = useUpdateQuota()
  const { data: projects } = useProjectOptions()
  const { data: clusters } = useClusterOptions()

  const [project, setProject] = useState(quota?.project ?? "")
  const [cluster, setCluster] = useState(quota?.cluster ?? EVERY_CLUSTER)
  const [deviceClass, setDeviceClass] = useState(quota?.device_class ?? "")
  const [guaranteed, setGuaranteed] = useState<number | null>(
    quota?.guaranteed_devices ?? 0
  )
  const [limit, setLimit] = useState<number | null>(
    quota?.limit_devices ?? null
  )
  const [weight, setWeight] = useState<number | null>(
    quota?.over_quota_weight ?? 1
  )
  const [overQuota, setOverQuota] = useState(quota?.over_quota_enabled ?? true)

  const pending = create.isPending || update.isPending
  const error = create.error ?? update.error

  const shared = {
    guaranteed_devices: guaranteed ?? 0,
    limit_devices: limit,
    over_quota_weight: weight ?? 1,
    over_quota_enabled: overQuota,
  }

  const done = (verb: string) => {
    onOpenChange(false)
    toast.success(`Quota ${verb}`, { description: deviceClass })
  }

  const submit = () => {
    if (editing) {
      update.mutate(
        { ref: quota.id, body: shared, revision: quota.revision },
        {
          onSuccess: () => done("updated"),
          onError: (e) => toast.error(formatError(e, "Could not update quota")),
        }
      )
      return
    }
    create.mutate(
      {
        project,
        device_class: deviceClass,
        cluster: cluster === EVERY_CLUSTER ? null : cluster,
        ...shared,
      },
      {
        onSuccess: () => done("created"),
        onError: (e) => toast.error(formatError(e, "Could not create quota")),
      }
    )
  }

  return (
    <FormDialog
      icon="quota"
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? "Edit quota" : "New quota"}
      description="A guaranteed floor and a fair share above it."
      error={error}
      pending={pending}
      disabled={!editing && (!project || !deviceClass)}
      onSubmit={submit}
    >
      {!editing && (
        <FormGrid>
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
            label="Cluster"
            value={cluster}
            onChange={setCluster}
            hint="Every cluster sums usage across all of them."
            options={[
              { value: EVERY_CLUSTER, label: "every cluster" },
              ...(clusters ?? []).map((c) => ({
                value: c.id,
                label: clusterLabel(c),
              })),
            ]}
          />
          <DeviceClassField value={deviceClass} onChange={setDeviceClass} />
        </FormGrid>
      )}

      <FormGrid>
        <NumberField
          label="Guaranteed devices"
          value={guaranteed}
          onChange={setGuaranteed}
          min={0}
        />
        <NumberField
          label="Limit devices"
          value={limit}
          onChange={setLimit}
          min={0}
          hint="Blank for no ceiling."
        />
        <NumberField
          label="Over-quota weight"
          value={weight}
          onChange={setWeight}
          hint="Relative share of what is left over."
        />
      </FormGrid>

      <SwitchField
        label="Over-quota allowed"
        hint="Whether this project may borrow beyond its guarantee."
        checked={overQuota}
        onChange={setOverQuota}
      />
    </FormDialog>
  )
}
