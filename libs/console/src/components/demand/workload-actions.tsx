import {
  FormDialog,
  FormGrid,
  NumberField,
  SelectField,
} from "@/components/layout/form"
import { formatError } from "@/lib/format"
import { scaleUnit } from "@/modules/demand/display"
import {
  groupDevices,
  isDisaggregated,
  rolesAtGroupCount,
  scaleUnitFor,
} from "@/modules/demand/topology"
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
  const unit = scaleUnitFor(workload) || scaleUnit(workload.kind)
  const devices = groupDevices(workload)
  const roles = rolesAtGroupCount(workload, desired ?? 0)

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
      {roles.length > 0 && (
        /*
         * A group count is not a replica count and the form must not let the
         * two be read as one. Three groups at 1:2 is nine processes and
         * seventy-two devices, and the arithmetic belongs in front of the
         * person doing it, not in the rejection they get back.
         *
         * This is also the cheap action of the two on this screen: scaling
         * multiplies the weights out, and nothing about a new prefill process
         * changes what a decode process is, so no weights reload.
         */
        <p className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs text-success">
          {roles.map((role) => `${role.count} ${role.role}`).join(" · ")}
          {devices ? ` · ${devices * (desired ?? 0)} devices` : ""}. Scaling
          groups reloads no model weights; the ratio is preserved by
          construction because both roles grow together.
        </p>
      )}
      <p className="text-xs text-muted-foreground">
        The workload's share on each cluster is derived, never typed. Nothing
        autoscales between the minimum and the maximum: they are the bounds
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

/**
 * Agg ↔ disagg, as an operation and not a field write.
 *
 * It is imperative, long-running and able to fail: it changes the device
 * footprint per unit, so it can sit pending on quota indefinitely, which is the
 * failure mode `workload.scale` already exists to carry. A PATCH would report
 * success the moment the plane accepted the intent, and "why has this model
 * been half-rolled for twenty minutes" would have no answer.
 *
 * The new group count is asked for and never inherited. A workload at three
 * replicas means three aggregated replicas today and would mean three GROUPS
 * (seventy-two devices at 1:2 and TP8) the moment the topology flips.
 * Reinterpreting a live number by a factor of three is the mistake this form
 * exists to make impossible, so a flip that does not say how many groups it
 * wants is rejected, not defaulted.
 */
export function ReshapeWorkloadDialog({
  workload,
  open,
  onOpenChange,
}: {
  workload: Workload
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const disagg = isDisaggregated(workload)
  const target = disagg ? "aggregated" : "disaggregated"
  const [groups, setGroups] = useState<number | null>(null)
  const [prefill, setPrefill] = useState<number | null>(1)
  const [decode, setDecode] = useState<number | null>(2)
  const [armed, setArmed] = useState(false)
  const { run, pending, error } = useOperation(workload, () =>
    onOpenChange(false)
  )

  const perWorker = workload.serving.shape.device_count
  const perUnit = disagg
    ? perWorker
    : ((prefill ?? 0) + (decode ?? 0)) * perWorker
  const unit = disagg ? "replicas" : "groups"

  return (
    <FormDialog
      icon="workload"
      open={open}
      onOpenChange={onOpenChange}
      title={`Reshape ${workload.name}`}
      description={`Serve this workload ${target}. This changes which processes exist, not how many of them there are.`}
      error={error}
      pending={pending}
      submitLabel={armed ? "Reshape and take the outage" : "Reshape"}
      disabled={
        groups === null ||
        (!disagg && ((prefill ?? 0) < 1 || (decode ?? 0) < 1))
      }
      onSubmit={() => {
        if (!armed) {
          setArmed(true)
          return
        }
        run(
          "workload.reshape",
          {
            topology: target,
            desired: groups ?? 0,
            ...(disagg
              ? {}
              : {
                  roles: [
                    { role: "prefill", weight: prefill ?? 1 },
                    { role: "decode", weight: decode ?? 1 },
                  ],
                }),
          },
          null
        )
      }}
    >
      <FormGrid>
        <NumberField
          label={`Desired ${unit}`}
          value={groups}
          onChange={(value) => {
            setArmed(false)
            setGroups(value)
          }}
          min={0}
          hint={`${workload.scale.desired} today, counted in ${scaleUnitFor(workload)}.`}
        />
        {!disagg && (
          <>
            <NumberField
              label="Prefill per group"
              value={prefill}
              onChange={(value) => {
                setArmed(false)
                setPrefill(value)
              }}
              min={1}
            />
            <NumberField
              label="Decode per group"
              value={decode}
              onChange={(value) => {
                setArmed(false)
                setDecode(value)
              }}
              min={1}
            />
          </>
        )}
      </FormGrid>
      <p className="text-xs text-muted-foreground">
        One {unit.replace(/s$/, "")} costs {perUnit} devices, at {perWorker} per
        worker. The count is asked for, not carried over, because the same
        number means a different amount of hardware on either side of this.
      </p>
      <p
        className={
          armed
            ? "rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-xs text-destructive"
            : "rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-warning"
        }
      >
        Every replica is replaced and every process reloads its weights. A
        rolling update needs room for both generations at once; without spare
        devices for that, expect the model to be down while it reloads.
        {armed ? " Press the button again to proceed." : ""}
      </p>
    </FormDialog>
  )
}
