import { JsonField, NumberField } from "@/components/layout/form"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import {
  recipeRoles,
  refusedRoleArgs,
  strandedByRatio,
} from "@/modules/demand/topology"
import type { ServingRole, WorkloadRole } from "@/modules/demand/types"
import { IconMinus, IconPlus } from "@tabler/icons-react"

/**
 * Declaring the split, before anything is running.
 *
 * Nothing here is destructive, there is no workload yet, so none of the
 * locking the detail page needs applies. What does apply is the arithmetic: a
 * ratio decides how many devices one indivisible unit costs, and a unit that
 * does not divide the hardware strands whole nodes without anything failing.
 * That is the one thing this form has to say out loud, because it is the one
 * thing nothing else will say.
 */

/**
 * Aggregated or disaggregated, and the ratio when it is the second.
 *
 * Choosing disaggregated is one click and lands NVIDIA's own recipe: 1:2 with
 * the per-role arguments already filled in. Making somebody assemble a ratio
 * and six arguments before they can try the feature would mean the common case
 * is a form-filling exercise, and the common case is the recipe.
 */
export function TopologyChoice({
  roles,
  onChange,
  deviceCount,
  deviceClass,
  budgetDevices,
}: {
  /** Null is aggregated. There is no third state. */
  roles: WorkloadRole[] | null
  onChange: (roles: WorkloadRole[] | null) => void
  /** Devices per WORKER (8 at TP8) never the group's total. */
  deviceCount: number
  deviceClass: string
  /** Free devices of this class, for the stranding arithmetic. */
  budgetDevices?: number
}) {
  const disaggregated = roles !== null
  const groupDevices = (roles ?? []).reduce(
    (total, role) => total + role.weight * deviceCount,
    0
  )
  const stranded = strandedByRatio(budgetDevices ?? 0, groupDevices)

  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-2">
        <TopologyOption
          selected={!disaggregated}
          onSelect={() => onChange(null)}
          title="Aggregated"
          detail={`One process per replica. ${deviceCount} ${deviceClass} each.`}
          note="What almost everything runs. Prefill and decode share a worker and no KV cache crosses the network."
        />
        <TopologyOption
          selected={disaggregated}
          onSelect={() => onChange(recipeRoles())}
          title="Disaggregated"
          detail="Prefill and decode as separate processes, exchanging KV cache over the network."
          note="Buys latency isolation and pays for it in KV capacity: each role loads its own copy of the weights."
        />
      </div>

      {roles && (
        <div className="space-y-2 rounded-md border p-3">
          <div className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
            Workers per group
          </div>
          {roles.map((role) => (
            <div key={role.role} className="flex items-center gap-3">
              <span className="w-16 font-mono text-xs">{role.role}</span>
              <span className="inline-flex items-center gap-1">
                <Button
                  variant="outline"
                  size="icon"
                  className="size-6"
                  aria-label={`fewer ${role.role}`}
                  disabled={role.weight <= 1}
                  onClick={() =>
                    onChange(reweight(roles, role.role, role.weight - 1))
                  }
                >
                  <IconMinus className="size-3" />
                </Button>
                <span className="w-6 text-center font-mono text-xs">
                  {role.weight}
                </span>
                <Button
                  variant="outline"
                  size="icon"
                  className="size-6"
                  aria-label={`more ${role.role}`}
                  onClick={() =>
                    onChange(reweight(roles, role.role, role.weight + 1))
                  }
                >
                  <IconPlus className="size-3" />
                </Button>
              </span>
              <span className="font-mono text-2xs text-muted-foreground">
                {role.weight * deviceCount} devices
              </span>
            </div>
          ))}

          <div className="font-mono text-2xs text-muted-foreground">
            one group = {groupDevices} {deviceClass} ·{" "}
            {roles.reduce((total, role) => total + role.weight, 0)} workers
          </div>

          {stranded && (
            /* Nothing on the server says this. The plane spells the sentence
               and cannot run it: devices-per-node is a fact about a cluster,
               and the declaration path has no cluster in hand, so a ratio that
               strands a node produces no error at declaration, no error at
               placement, and a bill. */
            <p className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-1.5 text-2xs text-warning">
              {stranded}. This is not refused anywhere: the group either fits or
              the devices sit held and idle.
            </p>
          )}

          <p className="text-2xs text-muted-foreground">
            The group is the unit of scale: the replica count on the next step
            counts groups, and both roles grow together, so the ratio cannot
            drift. Changing it later is free.
          </p>
        </div>
      )}
    </div>
  )
}

function reweight(
  roles: WorkloadRole[],
  role: ServingRole,
  weight: number
): WorkloadRole[] {
  return roles.map((row) =>
    row.role === role ? { ...row, weight: Math.max(1, weight) } : row
  )
}

function TopologyOption({
  selected,
  onSelect,
  title,
  detail,
  note,
}: {
  selected: boolean
  onSelect: () => void
  title: string
  detail: string
  note: string
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "space-y-1 rounded-lg border p-3 text-left transition-colors",
        selected ? "border-primary bg-primary/5" : "hover:bg-muted/40"
      )}
    >
      <div className="font-mono text-xs">{title}</div>
      <div className="text-2xs text-muted-foreground">{detail}</div>
      <div className="text-2xs text-muted-foreground">{note}</div>
    </button>
  )
}

/**
 * Per-role parameters, beside the workload's own.
 *
 * Prefilled from the recipe and every one of them editable, which is the same
 * bargain the rest of this flow makes: nobody should have to know the right
 * numbers, and anybody who does should be able to say so.
 *
 * `max_batch_tokens` is a field and not a line in the argument map because the
 * plane refuses `--max-num-batched-tokens` from a role's arguments: it is a
 * derived flag with a typed column. The two roles are sixteenfold apart on it
 * in the recipe, which is why it is the one number worth its own box.
 */
export function RoleParamFields({
  roles,
  onChange,
}: {
  roles: WorkloadRole[]
  onChange: (roles: WorkloadRole[]) => void
}) {
  const set = (role: ServingRole, patch: Partial<WorkloadRole>) =>
    onChange(
      roles.map((row) => (row.role === role ? { ...row, ...patch } : row))
    )

  return (
    <div className="space-y-3">
      {roles.map((role) => {
        const refused = refusedRoleArgs(role.engine_args)
        return (
          <div key={role.role} className="space-y-2 rounded-md border p-3">
            <div className="flex items-baseline justify-between">
              <span className="font-mono text-xs">{role.role}</span>
              <span className="font-mono text-2xs text-muted-foreground">
                ×{role.weight} per group
              </span>
            </div>
            <NumberField
              label="Max batch tokens"
              value={role.max_batch_tokens}
              onChange={(max_batch_tokens) =>
                set(role.role, { max_batch_tokens })
              }
              min={0}
              hint={
                role.role === "prefill"
                  ? "How much work one engine step does. Prefill runs long batches; the recipe uses 32768."
                  : "Decode steps are short and frequent; the recipe uses 2048."
              }
            />
            <JsonField
              label="Engine args"
              value={role.engine_args}
              rows={3}
              onChange={(value, ok) =>
                ok &&
                set(role.role, {
                  engine_args: value as Record<string, unknown>,
                })
              }
              hint="Only what differs from the workload's own arguments."
            />
            {refused.length > 0 && (
              <p className="rounded-md border border-destructive/50 bg-destructive/10 px-2.5 py-1.5 text-2xs text-destructive">
                {refused.join(", ")}: rendered from a declared column or from
                the topology. The plane refuses a role that sets one.
              </p>
            )}
          </div>
        )
      })}
      <p className="text-2xs text-muted-foreground">
        These decide what each process is, so they are in the serving digest.
        Changing them on a running workload reloads the weights in every worker
        of both roles: the ratio above is the part that stays free.
      </p>
    </div>
  )
}
