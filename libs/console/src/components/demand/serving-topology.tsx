import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldGrid } from "@/components/layout/field"
import { Dash } from "@/components/layout/mono"
import { StatusDot } from "@/components/layout/status-dot"
import { cn } from "@/lib/utils"
import {
  achievedRatio,
  declaredRatio,
  declaredRoles,
  groupDevices,
  roleDevices,
  roleOverrides,
  type RoleReadiness,
} from "@/modules/demand/topology"
import type { Workload, WorkloadDeployment } from "@/modules/demand/types"

/**
 * A disaggregated workload, as a person at a screen has to see it.
 *
 * The subject of every part here is one asymmetry: the ratio is free to change
 * and the per-role configuration is not. A ratio change is a scale and reloads
 * no weights; a change to what a role IS renames the objects Dynamo derives for
 * BOTH roles and costs two weight loads of about twenty minutes each. Nothing
 * on the screen may let those two look like the same kind of edit.
 */

/**
 * Whether the workload is served by one process per replica or two.
 *
 * On the detail panel it sits beside parallelism because it is the same kind of
 * fact — it decides the footprint. An aggregated workload gets no badge at all
 * rather than a grey one saying so: the overwhelming majority are aggregated,
 * and a badge on every one of them stops being a signal.
 */
export function TopologyBadge({ workload }: { workload: Workload }) {
  const ratio = declaredRatio(workload)
  if (!ratio) return null
  return (
    <Badge variant="outline" className="font-mono">
      disaggregated
    </Badge>
  )
}

/** One cluster's achieved ratio, named by the cluster it is true of. */
function AchievedLine({
  workload,
  deployment,
}: {
  workload: Workload
  deployment: WorkloadDeployment
}) {
  const achieved = achievedRatio(workload, deployment.status)
  if (!achieved) return null
  return (
    <div className="flex items-center gap-1.5 text-2xs text-muted-foreground">
      <StatusDot tone={achieved.settling ? "neutral" : "ok"} />
      <span>
        {achieved.label} on {deployment.cluster_name}
      </span>
      {achieved.settling && (
        <span className="text-muted-foreground/60">· settling</span>
      )}
    </div>
  )
}

/**
 * `1/1 prefill · 2/2 decode`, never `3 of 3`.
 *
 * The rollout state worth watching is precisely which role is short, and a
 * single number cannot carry it. One role at zero gets the loud treatment and
 * the others do not, because a deployment with every decode worker up and no
 * prefill worker is not serving degraded — it is serving nothing.
 */
export function RoleReadinessLine({
  roles,
  className,
}: {
  roles: RoleReadiness[]
  className?: string
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-2 font-mono text-xs",
        className
      )}
    >
      {roles.map((role) => (
        <span key={role.role} className="inline-flex items-center gap-1">
          <StatusDot
            tone={role.absent ? "danger" : role.complete ? "ok" : "warn"}
            pulse={role.absent}
          />
          <span className={role.absent ? "text-destructive" : undefined}>
            {role.ready}/{role.desired} {role.role}
          </span>
        </span>
      ))}
    </span>
  )
}

/**
 * What each role is, and what it costs — the declared half, not the observed one.
 *
 * Only what a role overrides is listed under it. Repeating the fields it
 * inherits from the workload is how somebody comes to believe a role declares a
 * value it does not, and then edits it in the wrong place.
 */
export function RolesPanel({
  workload,
  action,
}: {
  workload: Workload
  action?: React.ReactNode
}) {
  const roles = declaredRoles(workload)
  if (roles.length === 0) return null
  const devices = groupDevices(workload)

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between pb-2">
        <CardTitle className="text-sm">Roles</CardTitle>
        {action}
      </CardHeader>
      <CardContent className="space-y-3">
        <FieldGrid>
          <Field label="Ratio">
            <div className="space-y-1">
              <div>{declaredRatio(workload)} declared</div>
              {/* Achieved per cluster, because the quotient is a fact about one
                  deployment's pods and averaging two clusters would produce a
                  ratio no cluster is running. Clusters that report no roles
                  contribute no line rather than a blank one. */}
              {workload.deployments.map((deployment) =>
                deployment.status?.roles?.length ? (
                  <AchievedLine
                    key={deployment.id}
                    workload={workload}
                    deployment={deployment}
                  />
                ) : null
              )}
            </div>
          </Field>
          <Field label="Devices per group">
            {devices === null ? (
              <Dash>{null}</Dash>
            ) : (
              `${devices}× ${workload.serving.shape.device_class}`
            )}
          </Field>
        </FieldGrid>
        <div className="divide-y rounded-md border">
          {roles.map((role) => {
            const overrides = roleOverrides(role)
            return (
              <div key={role.role} className="space-y-1 px-3 py-2">
                <div className="flex items-baseline justify-between font-mono text-xs">
                  <span>{role.role}</span>
                  <span className="text-2xs text-muted-foreground">
                    ×{role.weight} per group · {roleDevices(workload, role)}{" "}
                    devices
                  </span>
                </div>
                {overrides.length === 0 ? (
                  <div className="font-mono text-2xs text-muted-foreground/60">
                    inherits every argument from the workload
                  </div>
                ) : (
                  <div className="flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-2xs text-muted-foreground">
                    {overrides.map((row) => (
                      <span key={row.key}>
                        {row.key}={row.value}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
        {workload.serving.topology?.group_nodes_hint && (
          /* The arithmetic a person is about to be surprised by, in the
             plane's own words. It is not a refusal and must not read as one:
             placement decides what fits, and this cannot — devices per node is
             a fact about a cluster, not about a device class. */
          <p className="rounded-md border bg-muted/50 px-3 py-2 text-2xs text-muted-foreground">
            {workload.serving.topology.group_nodes_hint}
          </p>
        )}
        <p className="text-2xs text-muted-foreground">
          The weights are scale: changing the ratio adds or removes workers and
          reloads no weights. Everything else on a role decides what process
          runs, and editing it restarts both roles.
        </p>
      </CardContent>
    </Card>
  )
}
