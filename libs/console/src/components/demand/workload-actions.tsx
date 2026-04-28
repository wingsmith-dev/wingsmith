import {
  FormDialog,
  FormGrid,
  NumberField,
  SelectField,
} from "@/components/layout/form"
import { formatError } from "@/lib/format"
import { scaleUnit } from "@/modules/demand/display"
import type { Workload, WorkloadDeployment } from "@/modules/demand/types"
import { useCreateOperation } from "@/modules/machinery/queries"
import { useState } from "react"
import { toast } from "sonner"

/**
 * Scale, pause and drain are operations, not field writes.
 *
 * Each of them is long-running and can fail: a scale-up sits pending while the
 * quota it needs is held by something else, a drain waits for replicas to go
 * away. Writing them as a PATCH would report success the moment the plane
 * accepted the intent, which is the one thing nobody wants to know. So each
 * dialog here posts `POST /v1/operations` and hands back an id with a phase.
 */
function useOperation(workload: Workload, onDone: () => void) {
  const create = useCreateOperation()
  const run = (
    type: string,
    parameters: Record<string, unknown>,
    reason: string | null
  ) =>
    create.mutate(
      { type, target: workload.id, parameters, reason },
      {
        onSuccess: (operation) => {
          onDone()
          toast.success(`${operation.type} requested`, {
            description: operation.id,
          })
        },
        onError: (error) =>
          toast.error(formatError(error, "Could not start operation")),
      }
    )
  return { run, pending: create.isPending, error: create.error }
}

/**
 * The totals, restated. `min` and `max` are sent alongside `desired` because
 * the plane validates the three together and a desired outside the bounds it
 * still holds is a rejection nobody could have predicted from this form.
 */
export function ScaleWorkloadDialog({
  workload,
  open,
  onOpenChange,
}: {
  workload: Workload
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [desired, setDesired] = useState<number | null>(workload.scale.desired)
  const [min, setMin] = useState<number | null>(workload.scale.min)
  const [max, setMax] = useState<number | null>(workload.scale.max)
  const { run, pending, error } = useOperation(workload, () =>
    onOpenChange(false)
  )
  const unit = workload.scale.unit || scaleUnit(workload.kind)

  return (
    <FormDialog
      icon="workload"
      open={open}
      onOpenChange={onOpenChange}
      title={`Scale ${workload.name}`}
      description={`Totals across every cluster this workload may use, counted in ${unit}. Placement divides them into per-cluster shares.`}
      error={error}
      pending={pending}
      submitLabel="Scale"
      disabled={desired === null}
      onSubmit={() =>
        run("workload.scale", { desired: desired ?? 0, min, max }, null)
      }
    >
      <FormGrid>
        <NumberField
          label={`Desired ${unit}`}
          value={desired}
          onChange={setDesired}
          min={0}
        />
        <NumberField label="Minimum" value={min} onChange={setMin} min={0} />
        <NumberField label="Maximum" value={max} onChange={setMax} min={0} />
      </FormGrid>
      <p className="text-xs text-muted-foreground">
        The workload's share on each cluster is derived, never typed. Nothing
        autoscales between the minimum and the maximum — they are the bounds
        placement is allowed to move within, and today it sits on the desired.
      </p>
    </FormDialog>
  )
}

/** Pausing drives every share to zero and leaves the deployments in place. */
export function PauseWorkloadDialog({
  workload,
  open,
  onOpenChange,
}: {
  workload: Workload
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { run, pending, error } = useOperation(workload, () =>
    onOpenChange(false)
  )
  const resuming = workload.paused

  return (
    <FormDialog
      icon="workload"
      open={open}
      onOpenChange={onOpenChange}
      title={resuming ? `Resume ${workload.name}` : `Pause ${workload.name}`}
      description={
        resuming
          ? "Restores each cluster's share from the declared totals."
          : "Drives every cluster's share to zero. The deployments stay, so resuming does not have to find capacity from scratch."
      }
      error={error}
      pending={pending}
      submitLabel={resuming ? "Resume" : "Pause"}
      onSubmit={() => run("workload.pause", { paused: !workload.paused }, null)}
    >
      <p className="text-xs text-muted-foreground">
        {workload.deployments.length} cluster
        {workload.deployments.length === 1 ? "" : "s"} affected.
      </p>
    </FormDialog>
  )
}

/**
 * Taking one cluster off a workload. Not a delete: the share goes to zero, the
 * endpoint targets go, and the row is removed once its replicas are gone.
 */
export function DrainWorkloadDialog({
  workload,
  deployment,
  open,
  onOpenChange,
}: {
  workload: Workload
  deployment?: WorkloadDeployment
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [cluster, setCluster] = useState(deployment?.cluster_id ?? "")
  const { run, pending, error } = useOperation(workload, () =>
    onOpenChange(false)
  )

  return (
    <FormDialog
      icon="workload"
      open={open}
      onOpenChange={onOpenChange}
      title={`Drain a cluster from ${workload.name}`}
      description="Its share goes to zero and its endpoint targets stop routing. The remaining clusters are re-expanded to carry the total."
      error={error}
      pending={pending}
      submitLabel="Drain"
      disabled={!cluster}
      onSubmit={() => run("workload.drain", { cluster }, null)}
    >
      <SelectField
        label="Cluster"
        value={cluster}
        onChange={setCluster}
        options={workload.deployments.map((d) => ({
          value: d.cluster_id,
          label: d.region ? `${d.cluster_name} · ${d.region}` : d.cluster_name,
        }))}
      />
    </FormDialog>
  )
}
