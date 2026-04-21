import {
  FormDialog,
  FormGrid,
  NumberField,
  SelectField,
  SwitchField,
} from "@/components/layout/form"
import { formatError } from "@/lib/format"
import { cn } from "@/lib/utils"
import {
  AVAILABILITY_OBJECTIVES,
  LIMIT_SCOPES,
  LIMIT_SCOPE_LABEL,
  LIMIT_SCOPE_MEANING,
  ON_EXCEED,
  ON_EXCEED_MEANING,
  beyondSafeInteger,
  formatObjective,
  queueTimeoutFor,
} from "@/modules/binding/display"
import {
  useCreateServingQuota,
  useServingEndpointOptions,
  useUpdateServingQuota,
} from "@/modules/binding/queries"
import type {
  LimitScope,
  OnExceed,
  ServingQuota,
} from "@/modules/binding/types"
import { useState } from "react"
import { toast } from "sonner"

/**
 * The commitment picker. Every limit is nullable and a blank one is
 * uncommitted rather than zero, so nothing here defaults a number into a field
 * an operator did not sell.
 *
 * The endpoint is offered only on create: it is the row's whole scope, and
 * moving it would withdraw a commitment from one contract and grant it to
 * another rather than edit this one.
 */
export function ServingQuotaForm({
  open,
  onOpenChange,
  quota,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  quota?: ServingQuota
}) {
  const editing = !!quota
  const create = useCreateServingQuota()
  const update = useUpdateServingQuota()
  const { data: endpoints } = useServingEndpointOptions()

  const [endpoint, setEndpoint] = useState(quota?.endpoint ?? "")
  const [rpm, setRpm] = useState<number | null>(quota?.committed_rpm ?? null)
  const [burst, setBurst] = useState<number | null>(quota?.burst_rpm ?? null)
  const [ramp, setRamp] = useState<number | null>(quota?.ramp_up_seconds ?? 0)
  const [tpm, setTpm] = useState<number | null>(quota?.committed_tpm ?? null)
  const [concurrent, setConcurrent] = useState<number | null>(
    quota?.max_concurrent_requests ?? null
  )
  const [maxOutput, setMaxOutput] = useState<number | null>(
    quota?.max_output_tokens ?? null
  )
  const [onExceed, setOnExceed] = useState<OnExceed>(
    quota?.on_exceed ?? "reject"
  )
  const [queueTimeout, setQueueTimeout] = useState<number | null>(
    quota?.queue_timeout_ms ?? null
  )
  const [scope, setScope] = useState<LimitScope>(
    quota?.limit_scope ?? "per_replica"
  )
  const [successRate, setSuccessRate] = useState<number | null>(
    quota?.success_rate_objective ?? null
  )
  const [availability, setAvailability] = useState(
    quota?.availability_objective ?? AVAILABILITY_OBJECTIVES[0]
  )
  const [objectiveWindow, setObjectiveWindow] = useState<number | null>(
    quota?.objective_window_seconds ?? 600
  )
  const [enforced, setEnforced] = useState(quota?.enforced ?? false)

  const pending = create.isPending || update.isPending
  const error = create.error ?? update.error
  const unreadable = [rpm, burst, tpm, concurrent, maxOutput].some(
    beyondSafeInteger
  )

  const body = {
    committed_rpm: rpm,
    committed_tpm: tpm,
    max_concurrent_requests: concurrent,
    max_output_tokens: maxOutput,
    burst_rpm: burst,
    ramp_up_seconds: ramp ?? 0,
    on_exceed: onExceed,
    queue_timeout_ms: queueTimeoutFor(onExceed, queueTimeout),
    limit_scope: scope,
    success_rate_objective: successRate,
    availability_objective: availability,
    objective_window_seconds: objectiveWindow ?? 600,
    enforced,
  }

  const done = (verb: string) => {
    onOpenChange(false)
    toast.success(`Serving quota ${verb}`, {
      description: enforced ? undefined : "Shadow mode — nothing is refused.",
    })
  }

  const submit = () => {
    if (editing) {
      update.mutate(
        { ref: quota.id, body, revision: quota.revision },
        {
          onSuccess: () => done("updated"),
          onError: (e) =>
            toast.error(formatError(e, "Could not update serving quota")),
        }
      )
      return
    }
    create.mutate(
      { endpoint, ...body },
      {
        onSuccess: () => done("created"),
        onError: (e) =>
          toast.error(formatError(e, "Could not create serving quota")),
      }
    )
  }

  return (
    <FormDialog
      icon="quota"
      wide
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? "Edit serving quota" : "New serving quota"}
      description="The traffic this endpoint is committed to carry, enforced by the gateway on every request."
      error={error}
      pending={pending}
      disabled={(!editing && !endpoint) || unreadable}
      onSubmit={submit}
    >
      {!editing && (
        <SelectField
          label="Endpoint"
          value={endpoint}
          onChange={setEndpoint}
          hint="One commitment per endpoint. An endpoint with no row is not rate limited at all."
          options={(endpoints ?? []).map((e) => ({
            value: e.id,
            label: e.name,
          }))}
        />
      )}

      <FormGrid>
        <NumberField
          label="Committed requests/min"
          value={rpm}
          onChange={setRpm}
          min={1}
          hint="Blank is uncommitted — requests are not limited."
        />
        <NumberField
          label="Committed tokens/min"
          value={tpm}
          onChange={setTpm}
          min={1}
          hint="Blank is uncommitted — tokens are not limited."
        />
        <NumberField
          label="Burst requests/min"
          value={burst}
          onChange={setBurst}
          min={1}
          hint="At or above the committed rate; blank for no burst allowance."
        />
        <NumberField
          label="Ramp-up seconds"
          value={ramp}
          onChange={setRamp}
          min={0}
        />
        <NumberField
          label="Max concurrent requests"
          value={concurrent}
          onChange={setConcurrent}
          min={1}
          hint="Blank is uncommitted."
        />
        <NumberField
          label="Max output tokens"
          value={maxOutput}
          onChange={setMaxOutput}
          min={1}
          hint="A ceiling on one request, so it never divides across replicas."
        />
      </FormGrid>

      {unreadable && (
        <p className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          A limit is larger than a number this console can hold exactly. Declare
          it with `hgr` rather than writing back a rounded figure.
        </p>
      )}

      <FormGrid>
        <SelectField
          label="Over the limit"
          value={onExceed}
          onChange={(value) => setOnExceed(value as OnExceed)}
          hint={ON_EXCEED_MEANING[onExceed]}
          options={ON_EXCEED}
        />
        {onExceed === "queue" && (
          <NumberField
            label="Queue timeout (ms)"
            value={queueTimeout}
            onChange={setQueueTimeout}
            min={1}
            hint="Required while queuing: it is what bounds the wait."
          />
        )}
      </FormGrid>

      <div className="space-y-2">
        <SelectField
          label="Limit scope"
          value={scope}
          onChange={(value) => setScope(value as LimitScope)}
          options={LIMIT_SCOPES.map((value) => ({
            value,
            label: LIMIT_SCOPE_LABEL[value],
          }))}
        />
        <div className="space-y-1.5 rounded-md border px-3 py-2.5">
          <p className="text-2xs text-muted-foreground/70">
            Gateway replicas keep no shared counter, so one of these two error
            directions has to be chosen.
          </p>
          {LIMIT_SCOPES.map((value) => (
            <p
              key={value}
              className={cn(
                "rounded-sm px-2 py-1 text-2xs",
                value === scope
                  ? "bg-muted text-foreground"
                  : "text-muted-foreground/50"
              )}
            >
              <span className="font-mono">{LIMIT_SCOPE_LABEL[value]}</span> —{" "}
              {LIMIT_SCOPE_MEANING[value]}
            </p>
          ))}
        </div>
      </div>

      <FormGrid>
        <NumberField
          label="Success-rate objective"
          value={successRate}
          onChange={setSuccessRate}
          hint={
            successRate === null
              ? "A fraction in (0, 1]. Blank is uncommitted."
              : `A fraction in (0, 1] — ${formatObjective(successRate)}.`
          }
        />
        <NumberField
          label="Objective window (s)"
          value={objectiveWindow}
          onChange={setObjectiveWindow}
          min={60}
          hint="Between 60 and 3600 seconds."
        />
      </FormGrid>

      <SelectField
        label="Availability objective"
        value={availability}
        onChange={setAvailability}
        hint="Which definition of a failed request the objective is read against."
        options={AVAILABILITY_OBJECTIVES}
      />

      <SwitchField
        label="Enforced"
        hint="Off is shadow mode: the gateway counts and reports these limits and never refuses a request."
        checked={enforced}
        onChange={setEnforced}
      />

      {!enforced && (
        <p className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          Shadow mode. Whatever is committed here, nothing is refused.
        </p>
      )}
    </FormDialog>
  )
}
