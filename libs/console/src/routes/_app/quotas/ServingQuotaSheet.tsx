import { Field, FieldGrid } from "@/components/layout/field"
import { NumberField } from "@/components/layout/form"
import { Id } from "@/components/layout/mono"
import { Badge } from "@/components/ui/badge"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { formatAbsolute, formatError } from "@/lib/format"
import {
  LIMIT_SCOPE_LABEL,
  LIMIT_SCOPE_MEANING,
  formatObjective,
  formatWindow,
} from "@/modules/binding/display"
import { useResolvedServingQuotas } from "@/modules/binding/queries"
import type { ServingQuota } from "@/modules/binding/types"
import { useState } from "react"
import {
  Committed,
  EnforcementBadge,
  OnExceedBadge,
  ScopeBadge,
  StandardBadge,
} from "./ServingLimits"

/**
 * One commitment in full, and what it means for a gateway fleet of a size the
 * operator names. The share arithmetic is asked of the plane rather than done
 * here: the console and the gateway must not be able to disagree about what a
 * replica enforces.
 */
export function ServingQuotaSheet({
  quota,
  onOpenChange,
}: {
  quota: ServingQuota
  onOpenChange: (open: boolean) => void
}) {
  const [replicas, setReplicas] = useState<number | null>(1)
  const fleet = replicas && replicas >= 1 ? replicas : 1
  const resolved = useResolvedServingQuotas({
    endpoint: quota.endpoint,
    replicas: fleet,
    replica_index: 0,
  })
  const row = resolved.data?.quotas.find((q) => q.endpoint === quota.endpoint)

  return (
    <Sheet open onOpenChange={onOpenChange}>
      <SheetContent className="w-full gap-0 overflow-y-auto sm:max-w-lg">
        <SheetHeader>
          <SheetTitle className="font-mono text-sm">
            {quota.endpoint_name}
          </SheetTitle>
          <SheetDescription>
            Traffic committed on this endpoint, enforced by the gateway on every
            request. Nothing on this panel is a measurement.
          </SheetDescription>
        </SheetHeader>

        <div className="space-y-5 px-4 pb-6">
          <div className="flex flex-wrap items-center gap-2">
            <EnforcementBadge enforced={quota.enforced} />
            <ScopeBadge scope={quota.limit_scope} />
            <StandardBadge standard={quota.standard} />
          </div>

          {!quota.enforced && (
            <p className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              Shadow mode. Every number below is counted and reported, and none
              of it is refused.
            </p>
          )}

          <section className="space-y-2">
            <h3 className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
              Declared
            </h3>
            <FieldGrid>
              <Field label="Requests/min">
                <Committed value={quota.committed_rpm} />
              </Field>
              <Field label="Tokens/min">
                <Committed value={quota.committed_tpm} />
              </Field>
              <Field label="Burst requests/min">
                <Committed value={quota.burst_rpm} />
              </Field>
              <Field label="Ramp-up">{quota.ramp_up_seconds}s</Field>
              <Field label="Max concurrent">
                <Committed value={quota.max_concurrent_requests} />
              </Field>
              <Field label="Max output tokens">
                <Committed value={quota.max_output_tokens} />
              </Field>
              <Field label="Over the limit">
                <OnExceedBadge
                  onExceed={quota.on_exceed}
                  queueTimeoutMs={quota.queue_timeout_ms}
                />
              </Field>
              <Field label="Success rate">
                {quota.success_rate_objective === null ? (
                  <Committed value={null} />
                ) : (
                  formatObjective(quota.success_rate_objective)
                )}
              </Field>
              <Field label="Availability objective">
                <Badge variant="outline" className="font-mono text-2xs">
                  {quota.availability_objective}
                </Badge>
              </Field>
              <Field label="Objective window">
                {formatWindow(quota.objective_window_seconds)}
              </Field>
            </FieldGrid>
          </section>

          <section className="space-y-3">
            <h3 className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
              What a replica enforces
            </h3>
            <p className="text-2xs text-muted-foreground">
              {LIMIT_SCOPE_MEANING[quota.limit_scope]}
            </p>
            <NumberField
              label="Gateway replicas"
              value={replicas}
              onChange={setReplicas}
              min={1}
              hint="The plane never discovers the fleet size on its own, so the shares are taken against the number stated here."
            />

            {!!resolved.error && (
              <p className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
                {formatError(resolved.error, "Could not resolve the fleet")}
              </p>
            )}

            {row && (
              <div className="space-y-3">
                <FieldGrid>
                  <Field label={`Requests/min · replica 0 of ${row.replicas}`}>
                    <Committed value={row.effective.committed_rpm} />
                  </Field>
                  <Field label={`Tokens/min · replica 0 of ${row.replicas}`}>
                    <Committed value={row.effective.committed_tpm} />
                  </Field>
                  <Field label="Burst/min · replica 0">
                    <Committed value={row.effective.burst_rpm} />
                  </Field>
                  <Field label="Max concurrent · replica 0">
                    <Committed value={row.effective.max_concurrent_requests} />
                  </Field>
                </FieldGrid>

                {row.starved_replicas > 0 && (
                  <p className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-warning">
                    {row.starved_replicas} of {row.replicas} replicas would hold
                    a share of zero on some limit and refuse every request that
                    reaches them. The commitment is smaller than the fleet.
                  </p>
                )}

                {quota.limit_scope === "per_replica" && (
                  <p className="text-2xs text-muted-foreground">
                    Under {LIMIT_SCOPE_LABEL.per_replica} these are the declared
                    numbers unchanged, and a fleet of {row.replicas} admits up
                    to {row.replicas} times what was sold.
                  </p>
                )}

                {row.workloads.length > 0 && (
                  <p className="text-2xs text-muted-foreground">
                    Traffic admitted through this name reaches{" "}
                    {row.workloads.length} workload
                    {row.workloads.length === 1 ? "" : "s"}. A workload behind
                    two endpoints carries two commitments and they are never
                    summed.
                  </p>
                )}
              </div>
            )}
          </section>

          <section className="space-y-2">
            <FieldGrid>
              <Field label="Declared at">
                {formatAbsolute(quota.created_at)}
              </Field>
              <Field label="Last change">
                {formatAbsolute(quota.updated_at)}
              </Field>
            </FieldGrid>
            <Id value={quota.id} />
          </section>
        </div>
      </SheetContent>
    </Sheet>
  )
}
