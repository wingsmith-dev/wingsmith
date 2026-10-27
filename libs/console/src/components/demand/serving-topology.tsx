import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { JsonField, NumberField } from "@/components/layout/form"
import { WriteAction } from "@/components/layout/write-action"
import { formatError } from "@/lib/format"
import { useUpdateWorkload } from "@/modules/demand/queries"
import {
  IconLock,
  IconLockOpen,
  IconMinus,
  IconPlus,
} from "@tabler/icons-react"
import { useState } from "react"
import { toast } from "sonner"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldGrid } from "@/components/layout/field"
import { Dash } from "@/components/layout/mono"
import { StatusDot } from "@/components/layout/status-dot"
import { cn } from "@/lib/utils"
import {
  achievedRatio,
  declaredRatio,
  declaredRoles,
  groupDevices,
  refusedRoleArgs,
  roleOverrides,
  rolloutCost,
  strandedByRatio,
  type RoleReadiness,
  type RolloutCost,
} from "@/modules/demand/topology"
import type { Workload, WorkloadDeployment } from "@/modules/demand/types"

/**
 * A disaggregated workload, as a person at a screen has to see it.
 *
 * The subject of every part here is one asymmetry: the ratio is free to change
 * and the per-role configuration is not. A ratio change is a scale and reloads
 * no weights; a change to what a role IS renames the objects Dynamo derives for
 * BOTH roles and reloads the weights in every worker of both. Nothing
 * on the screen may let those two look like the same kind of edit.
 */

/**
 * Whether the workload is served by one process per replica or two.
 *
 * On the detail panel it sits beside parallelism because it is the same kind of
 * fact: it decides the footprint. An aggregated workload gets no badge at all
 * instead of a gray one saying so: the overwhelming majority are aggregated,
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
        <span className="text-muted-foreground">· settling</span>
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
 * prefill worker is not serving degraded: it is serving nothing.
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
 * The ratio, as the cheap thing it is: two steppers and one Apply.
 *
 * Free in the literal sense: the weights are outside `serving_digest`, so a
 * change here is a scale. Rendering prefill's count into prefill's component
 * leaves decode's component byte-identical, byte-identical components keep
 * their names, and pods that keep their names do not restart. It was verified
 * on hardware, not reasoned about: prefill 1 to 3 renamed nothing and
 * left the decode Deployment on its original UID, and the warm path holds too,
 * so an added prefill worker cannot cold-start the cache the others are using.
 *
 * So there is no confirmation on this path and there must not be one. This is
 * the action we expect somebody to take often, it is the point of the
 * feature, and a confirmation here would flatten the distinction the panel
 * below is built to draw.
 */
function RatioControl({
  workload,
  budgetDevices,
  onApplied,
}: {
  workload: Workload
  budgetDevices?: number
  /** Closes the dialog the control was opened in, when it is in one. */
  onApplied?: () => void
}) {
  const declared = declaredRoles(workload)
  const update = useUpdateWorkload()
  const [weights, setWeights] = useState<Record<string, number>>(() =>
    Object.fromEntries(declared.map((role) => [role.role, role.weight]))
  )

  const edited = declared.map((role) => ({
    ...role,
    weight: weights[role.role] ?? role.weight,
  }))
  const changed = edited.some((role, i) => role.weight !== declared[i].weight)
  const groups = workload.scale.desired
  const perWorker = workload.serving.shape.device_count
  const devices = edited.reduce(
    (total, role) => total + role.weight * perWorker,
    0
  )
  const stranded = strandedByRatio(budgetDevices ?? 0, devices)

  const apply = () =>
    update.mutate(
      {
        ref: workload.id,
        body: {
          serving: {
            topology: {
              serving_topology: "disaggregated",
              /*
               * The WHOLE role, with only the weight moved.
               *
               * The block replaces whole and the plane reads each row straight
               * off the request, so an omitted `max_batch_tokens` stores null
               * and an omitted `engine_args` stores `{}`. Sending weights alone
               * would therefore wipe what makes prefill different from decode,
               * change `serving_digest`, and turn the one action this panel
               * promises is free into a full reload of both roles. The promise
               * is only true if this sends everything it is not changing.
               */
              roles: edited.map((role) => ({
                role: role.role,
                weight: role.weight,
                max_batch_tokens: role.max_batch_tokens,
                engine_args: role.engine_args,
              })),
            },
          },
        },
        revision: workload.revision,
      },
      {
        onSuccess: () => {
          onApplied?.()
          toast.success("Ratio applied", {
            description: `${devices} devices per group`,
          })
        },
        onError: (error) =>
          toast.error(formatError(error, "Could not change the ratio")),
      }
    )

  return (
    <div className="space-y-2 rounded-md border p-3">
      <div className="flex items-baseline justify-between">
        <span className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
          Ratio
        </span>
        <span className="text-2xs text-success">free · reloads nothing</span>
      </div>

      <div className="space-y-1.5">
        {edited.map((role) => (
          <div key={role.role} className="flex items-center gap-3">
            <span className="w-16 font-mono text-xs">{role.role}</span>
            <Stepper
              label={role.role}
              value={role.weight}
              onChange={(weight) =>
                setWeights((current) => ({ ...current, [role.role]: weight }))
              }
            />
            <span className="font-mono text-2xs text-muted-foreground">
              → {role.weight * groups} worker
              {role.weight * groups === 1 ? "" : "s"}
            </span>
          </div>
        ))}
      </div>

      <div className="font-mono text-2xs text-muted-foreground">
        {groups} group{groups === 1 ? "" : "s"} · {devices} devices per group ·{" "}
        {devices * groups} in total
      </div>

      {stranded && (
        /* Said here because nothing else will say it. The plane spells this
           arithmetic and cannot run it: the declaration path has no device
           budget, so a ratio that strands a node produces no error anywhere,
           which is precisely why it is worth a line at the moment of choosing. */
        <p className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-1.5 text-2xs text-warning">
          {stranded}. Nothing refuses this: the devices are simply held and
          idle.
        </p>
      )}

      <WriteAction>
        <Button
          size="sm"
          className="w-full"
          disabled={!changed || update.isPending}
          onClick={apply}
        >
          {changed
            ? `Apply ${edited.map((role) => `${role.weight} ${role.role}`).join(" : ")}`
            : "Apply ratio"}
        </Button>
      </WriteAction>
    </div>
  )
}

/** A minus/plus pair. Weights are small integers and a text box invites a typo. */
function Stepper({
  label,
  value,
  onChange,
}: {
  /** The role, so the two steppers are tellable apart by name. */
  label: string
  value: number
  onChange: (value: number) => void
}) {
  return (
    <span className="inline-flex items-center gap-1">
      <Button
        variant="outline"
        size="icon"
        className="size-6"
        aria-label={`fewer ${label}`}
        disabled={value <= 1}
        onClick={() => onChange(value - 1)}
      >
        <IconMinus className="size-3" />
      </Button>
      <span className="w-6 text-center font-mono text-xs">{value}</span>
      <Button
        variant="outline"
        size="icon"
        className="size-6"
        aria-label={`more ${label}`}
        onClick={() => onChange(value + 1)}
      >
        <IconPlus className="size-3" />
      </Button>
    </span>
  )
}

/**
 * What the roll actually costs this workload, computed from its own counts.
 *
 * Never a fixed sentence. "Three workers reload" is right for 1P:2D at one
 * group and wrong for every other shape, and a number that is sometimes wrong
 * teaches people to skip the line it sits on.
 *
 * The first sentence is whether the model serves, because that is the whole
 * decision. Rolling happens in place, one worker at a time within a role, so
 * a role with two or more workers keeps one up and the model runs slower, while
 * a role with exactly one goes to zero, and a decode worker with no prefill peer
 * has nothing to decode from. Those are not two shades of the same warning; they
 * are "do it now" and "book a window".
 */
function RollCost({ cost }: { cost: RolloutCost }) {
  const counts = cost.perRole
    .map((role) => `${role.workers} ${role.role}`)
    .join(" and ")
  return (
    <div className="space-y-1.5">
      {cost.servesThrough ? (
        <p className="font-medium">
          The model keeps serving, slower. Workers roll one at a time, so one{" "}
          {cost.perRole[0]?.role} and one{" "}
          {cost.perRole[cost.perRole.length - 1]?.role} stay up throughout.
        </p>
      ) : (
        <p className="font-medium">
          The model stops serving for the whole reload. There is one{" "}
          {cost.roleAtRisk} worker, and while it reloads there is no{" "}
          {cost.roleAtRisk} process at all: every request fails, instead of
          running slower.
        </p>
      )}
      <p>
        Both roles are renamed and all {cost.workers} workers reload the
        model&rsquo;s weights: {counts}. A role&rsquo;s workers reload one after
        another, so the roll lasts {cost.longest.workers} weight{" "}
        {cost.longest.workers === 1 ? "load" : "loads"} end to end, set by{" "}
        {cost.longest.role}. Dynamo hashes one worker spec across the whole
        graph, so editing either role replaces the other role&rsquo;s pods too.
      </p>
    </div>
  )
}

/**
 * What each role IS: locked, because the lock is the design.
 *
 * These fields and the ratio above sit in one panel and cost radically
 * different things: the ratio adds workers and reloads nothing, and these
 * rename every pod in the graph and reload every one of them. Two controls that
 * look alike and are not is the failure this panel exists to prevent, and it
 * prevents it spatially: a stray keystroke cannot reach a locked field, so the
 * two paths are not the same gesture in the same place.
 *
 * Unlocking is deliberate and recoverable, which is the right size of gesture
 * for a painful, survivable outage. Type-the-name is the gesture for losing
 * data and this loses none.
 */
function RoleConfigPanel({ workload }: { workload: Workload }) {
  const declared = declaredRoles(workload)
  const cost = rolloutCost(workload)
  const update = useUpdateWorkload()
  const [unlocked, setUnlocked] = useState(false)
  const [batch, setBatch] = useState<Record<string, number | null>>(() =>
    Object.fromEntries(declared.map((r) => [r.role, r.max_batch_tokens]))
  )
  const [args, setArgs] = useState<Record<string, Record<string, unknown>>>(
    () =>
      Object.fromEntries(declared.map((r) => [r.role, { ...r.engine_args }]))
  )
  const [valid, setValid] = useState(true)

  const refused = declared.flatMap((role) =>
    refusedRoleArgs(args[role.role] ?? {}).map((key) => `${role.role}: ${key}`)
  )
  const changed = declared.some(
    (role) =>
      (batch[role.role] ?? null) !== (role.max_batch_tokens ?? null) ||
      JSON.stringify(args[role.role] ?? {}) !== JSON.stringify(role.engine_args)
  )

  const save = () =>
    update.mutate(
      {
        ref: workload.id,
        body: {
          serving: {
            topology: {
              serving_topology: "disaggregated",
              /* The weights ride along unchanged. The block replaces whole,
                 the way every sub-block of `serving` does, so omitting them
                 would read as a request to reset the ratio, not to
                 leave it alone. */
              roles: declared.map((role) => ({
                role: role.role,
                weight: role.weight,
                max_batch_tokens: batch[role.role] ?? null,
                engine_args: args[role.role] ?? {},
              })),
            },
          },
        },
        revision: workload.revision,
      },
      {
        onSuccess: () => {
          setUnlocked(false)
          toast.success("Role configuration updated", {
            description: `${workload.name} is reloading`,
          })
        },
        onError: (error) =>
          toast.error(formatError(error, "Could not update the roles")),
      }
    )

  return (
    <div className="space-y-2 rounded-md border p-3">
      <div className="flex items-baseline justify-between">
        <span className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
          Configuration
        </span>
        <span className="inline-flex items-center gap-1 text-2xs text-muted-foreground">
          {unlocked ? (
            <IconLockOpen className="size-3" />
          ) : (
            <IconLock className="size-3" />
          )}
          {unlocked ? "unlocked" : "locked"}
        </span>
      </div>

      <div className="divide-y rounded-md border">
        {declared.map((role) => (
          <div key={role.role} className="space-y-2 px-3 py-2">
            <div className="font-mono text-xs">{role.role}</div>
            {unlocked ? (
              <>
                <NumberField
                  label="Max batch tokens"
                  value={batch[role.role] ?? null}
                  onChange={(value) =>
                    setBatch((current) => ({ ...current, [role.role]: value }))
                  }
                  min={0}
                  hint="Blank inherits the workload's own value."
                />
                <JsonField
                  label="Engine args"
                  value={args[role.role] ?? {}}
                  rows={3}
                  onChange={(value, ok) => {
                    setValid(ok)
                    if (ok)
                      setArgs((current) => ({
                        ...current,
                        [role.role]: value as Record<string, unknown>,
                      }))
                  }}
                />
              </>
            ) : (
              <div className="flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-2xs text-muted-foreground">
                {roleOverrides(role).length === 0 ? (
                  <span className="text-muted-foreground">
                    inherits every argument from the workload
                  </span>
                ) : (
                  roleOverrides(role).map((row) => (
                    <span key={row.key}>
                      {row.key}={row.value}
                    </span>
                  ))
                )}
              </div>
            )}
          </div>
        ))}
      </div>

      {refused.length > 0 && (
        <p className="rounded-md border border-destructive/50 bg-destructive/10 px-2.5 py-1.5 text-2xs text-destructive">
          {refused.join(", ")}: rendered from a declared column or from the
          topology, so a value here contradicts a decision already acted on. The
          plane refuses it.
        </p>
      )}

      {cost && (
        <div className="space-y-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-2xs text-warning">
          <RollCost cost={cost} />
          {!unlocked && (
            <WriteAction>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setUnlocked(true)}
              >
                <IconLockOpen className="size-3" />
                Unlock to retune
              </Button>
            </WriteAction>
          )}
        </div>
      )}

      {unlocked && (
        <div className="flex gap-2">
          <Button
            variant="destructive"
            size="sm"
            disabled={
              !changed || !valid || refused.length > 0 || update.isPending
            }
            onClick={save}
          >
            {cost?.servesThrough
              ? `Reload ${cost.workers} workers`
              : `Reload ${cost?.workers ?? 0} workers · model down`}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setUnlocked(false)
              setBatch(
                Object.fromEntries(
                  declared.map((r) => [r.role, r.max_batch_tokens])
                )
              )
              setArgs(
                Object.fromEntries(
                  declared.map((r) => [r.role, { ...r.engine_args }])
                )
              )
            }}
          >
            Cancel
          </Button>
        </div>
      )}
    </div>
  )
}

/**
 * What each role is, and what it costs: the declared half, not the observed one.
 *
 * Two zones, and the split between them is the argument: the ratio is free and
 * lives behind one click, the configuration is destructive and lives behind a
 * lock. Putting them in one panel is deliberate, they are both "what the roles
 * are", and somebody looking for one will find the other, but they must not
 * look like one control.
 */
export function RolesPanel({
  workload,
  budgetDevices,
}: {
  workload: Workload
  /**
   * Devices this workload can expect to have, for the stranding arithmetic.
   * Absent means no budget is known and nothing is said, instead of a guess.
   */
  budgetDevices?: number
}) {
  const roles = declaredRoles(workload)
  if (roles.length === 0) return null
  const devices = groupDevices(workload)

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Roles</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <FieldGrid>
          <Field label="Declared">
            <div className="space-y-1">
              <div>{declaredRatio(workload)}</div>
              {/* Achieved per cluster, because the quotient is a fact about one
                  deployment's pods and averaging two clusters would produce a
                  ratio no cluster is running. Clusters that report no roles
                  contribute no line instead of a blank one. */}
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

        <RatioControl workload={workload} budgetDevices={budgetDevices} />
        <RoleConfigPanel workload={workload} />
      </CardContent>
    </Card>
  )
}

/**
 * The ratio on its own, from the page header.
 *
 * Not a `FormDialog`, which would put a Save beside the control's own Apply and
 * make a one-click action a two-click one. The control is the whole dialog.
 */
export function RatioDialog({
  workload,
  budgetDevices,
  open,
  onOpenChange,
}: {
  workload: Workload
  budgetDevices?: number
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Ratio</DialogTitle>
          <DialogDescription>
            How many of each role a group holds. Scale multiplies these; it does
            not change them.
          </DialogDescription>
        </DialogHeader>
        <RatioControl
          workload={workload}
          budgetDevices={budgetDevices}
          onApplied={() => onOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  )
}
