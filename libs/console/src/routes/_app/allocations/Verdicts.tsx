import { cn } from "@/lib/utils"
import { formatError } from "@/lib/format"
import type { ClusterVerdict, Share } from "@/modules/placement/types"
import { IconAlertTriangle, IconCheck, IconX } from "@tabler/icons-react"

/**
 * The plane's reasoning, shown as it came.
 *
 * Every line here is the decision's own text. The console does not summarise a
 * rejection or re-word a message: the plane returns `code`, `message` and the
 * numbers it computed from precisely so that "why not iad-1" has one answer
 * everywhere it is asked, and a console paraphrase is how that stops being true.
 */
export function Verdicts({
  verdicts,
  shares,
  shortfall,
  summary,
  error,
  isLoading,
  disagreement,
}: {
  verdicts: ClusterVerdict[] | undefined
  shares?: Share[]
  shortfall?: number
  summary?: string
  error?: unknown
  isLoading: boolean
  /**
   * What the board's own arithmetic found, when it differs from the decision
   * above. Stated rather than reconciled: the plane is the authority on whether
   * something is admitted, the board is looking at devices, and a difference is
   * worth seeing — it means one of them is reading a staler fleet, or that the
   * question each answered was not quite the same question.
   */
  disagreement?: string
}) {
  if (isLoading) {
    return (
      <p className="px-1.5 py-1 font-mono text-2xs text-muted-foreground/50">
        asking the plane…
      </p>
    )
  }
  if (error) {
    return (
      <p className="flex items-start gap-1.5 px-1.5 py-1 font-mono text-2xs text-amber-600 dark:text-amber-400">
        <IconAlertTriangle className="mt-px size-3 shrink-0" />
        {formatError(error)}
      </p>
    )
  }
  if (!verdicts) return null

  const feasible = verdicts.filter((verdict) => verdict.feasible)
  const refused = verdicts.filter((verdict) => !verdict.feasible)

  return (
    <div className="space-y-1.5 border-l-2 border-primary/30 py-1 pl-2">
      <div className="font-mono text-2xs tracking-wider text-primary/70 uppercase">
        the plane says
      </div>

      {summary && (
        <p className="font-mono text-2xs leading-relaxed text-muted-foreground/70">
          {summary}
        </p>
      )}

      {shares && shares.length > 0 && (
        <div className="space-y-0.5">
          {shares.map((share) => (
            <div
              key={share.cluster}
              className="flex items-baseline gap-1.5 font-mono text-2xs"
            >
              <IconCheck className="size-3 shrink-0 text-emerald-500" />
              <span className="flex-1 truncate">{share.cluster_name}</span>
              <span className="tabular-nums">
                {share.replicas} {share.replicas === 1 ? "replica" : "replicas"}
              </span>
            </div>
          ))}
          {!!shortfall && shortfall > 0 && (
            <div className="flex items-baseline gap-1.5 font-mono text-2xs text-amber-600 dark:text-amber-400">
              <IconAlertTriangle className="size-3 shrink-0" />
              <span className="flex-1">shortfall</span>
              <span className="tabular-nums">{shortfall}</span>
            </div>
          )}
        </div>
      )}

      {feasible.map((verdict) => (
        <Row key={verdict.cluster} verdict={verdict} />
      ))}
      {refused.map((verdict) => (
        <Row key={verdict.cluster} verdict={verdict} />
      ))}

      {disagreement && (
        <p className="border-t pt-1 font-mono text-2xs leading-relaxed text-muted-foreground/50">
          {disagreement}
        </p>
      )}
    </div>
  )
}

/**
 * `reserved` and `available` appear only when something is owed, and when they
 * do they sit between `free` and everything else — because they are the step
 * that turns the observed number into the one the decision was made on. A
 * refusal reading "8 free" beside "8 needed" is a decision that looks wrong
 * until the six devices already promised to somebody else are on the same line.
 */
function Row({ verdict }: { verdict: ClusterVerdict }) {
  const counts = [
    `${verdict.free_devices} free`,
    verdict.reserved_devices ? `${verdict.reserved_devices} reserved` : null,
    verdict.reserved_devices ? `${verdict.available_devices} available` : null,
    verdict.largest_domain_free_devices
      ? `largest domain ${verdict.largest_domain_free_devices}`
      : null,
    verdict.quota_headroom === null ? null : `quota ${verdict.quota_headroom}`,
    verdict.fit_slack ? `slack ${verdict.fit_slack}` : null,
  ]
    .filter(Boolean)
    .join(" · ")

  return (
    <div className="space-y-0.5">
      <div className="flex items-baseline gap-1.5 font-mono text-2xs">
        {verdict.feasible ? (
          <IconCheck className="size-3 shrink-0 text-emerald-500" />
        ) : (
          <IconX className="size-3 shrink-0 text-muted-foreground/40" />
        )}
        <span
          className={cn(
            "flex-1 truncate",
            !verdict.feasible && "text-muted-foreground/60"
          )}
        >
          {verdict.cluster_name}
        </span>
        <span className="shrink-0 text-muted-foreground/50">{counts}</span>
      </div>

      {verdict.reasons.map((reason) => (
        <div key={reason.code} className="pl-4">
          <span className="font-mono text-2xs text-amber-600 dark:text-amber-400">
            {reason.code}
          </span>
          <p className="font-mono text-2xs leading-relaxed text-muted-foreground/50">
            {reason.message}
          </p>
        </div>
      ))}

      {verdict.preemption && verdict.preemption.victims.length > 0 && (
        <div className="pl-4">
          <span className="font-mono text-2xs text-destructive">
            would preempt {verdict.preemption.victims.length}{" "}
            {verdict.preemption.victims.length === 1 ? "workload" : "workloads"}{" "}
            · {verdict.preemption.devices_reclaimed} devices
          </span>
        </div>
      )}
    </div>
  )
}
