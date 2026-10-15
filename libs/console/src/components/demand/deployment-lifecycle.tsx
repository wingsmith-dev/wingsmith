import { cn } from "@/lib/utils"
import { formatRelative } from "@/lib/format"
import { StatusDot } from "@/components/layout/status-dot"
import {
  LIFECYCLE_STEPS,
  lifecycleStepFor,
  reasonText,
} from "@/modules/demand/display"
import type { Deployment } from "@/modules/demand/types"

/**
 * Where a deployment is between declared and serving, and what is holding it there.
 *
 * The strip exists because "placed" was the last thing the console could say about a
 * deployment for as long as it took to come up — which is minutes when weights are
 * cold and forever when something is wrong. The cluster knew the answer the whole
 * time and reported it to a log in another cluster; this is that answer, on screen.
 *
 * Nothing here is computed from declared state. Every step's status comes from what
 * the cluster last reported, so a strip that says "Applied" means a cluster said so
 * and not that the plane believes it should have.
 */
export function DeploymentLifecycle({
  deployment,
}: {
  deployment: Deployment
}) {
  const status = deployment.status

  if (!status) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">
        <StatusDot tone="neutral" />
        No cluster has reported on this deployment yet.
      </div>
    )
  }

  const failed = status.phase === "failed"
  const degraded = status.phase === "degraded"
  const current = lifecycleStepFor(status.phase, status.reason)
  const currentIndex = LIFECYCLE_STEPS.findIndex((s) => s.key === current)

  // A step is settled only when the deployment is resting on it. `Serving` reached while
  // replicas are still filling in is the state a rolling update passes through and the one a
  // crash-looping replica sits in, and drawing it the same green as a finished rollout is how
  // the strip used to report a half-dead deployment as healthy.
  const settled = current === "serving" && !degraded && !failed

  return (
    <div className="space-y-2 rounded-md border p-3">
      <ol className="flex flex-wrap items-center gap-1">
        {LIFECYCLE_STEPS.map((step, i) => {
          const done = i < currentIndex
          const here = i === currentIndex
          return (
            <li key={step.key} className="flex items-center gap-1">
              <span
                className={cn(
                  "flex items-center gap-1.5 rounded px-2 py-1 text-2xs",
                  done && "text-muted-foreground",
                  here &&
                    !failed &&
                    !degraded &&
                    "bg-primary/10 font-medium text-primary",
                  here &&
                    degraded &&
                    "bg-amber-500/10 font-medium text-warning",
                  here &&
                    failed &&
                    "bg-destructive/10 font-medium text-destructive",
                  !done && !here && "text-muted-foreground"
                )}
              >
                <StatusDot
                  tone={
                    !here
                      ? done
                        ? "ok"
                        : "neutral"
                      : failed
                        ? "danger"
                        : degraded
                          ? "warn"
                          : settled
                            ? "ok"
                            : "info"
                  }
                  pulse={here && !failed && !settled}
                />
                {step.label}
              </span>
              {i < LIFECYCLE_STEPS.length - 1 && (
                <span
                  aria-hidden
                  className={cn(
                    "h-px w-4",
                    done ? "bg-emerald-500/40" : "bg-border"
                  )}
                />
              )}
            </li>
          )
        })}
      </ol>

      {status.reason && (
        <p
          className={cn(
            "text-xs",
            failed ? "text-destructive" : "text-muted-foreground"
          )}
        >
          {reasonText(status.reason)}
          {status.message && (
            <span className="text-muted-foreground"> — {status.message}</span>
          )}
        </p>
      )}

      {status.last_error_code && (
        <p className="text-xs text-destructive">
          {status.last_error_code}
          {status.last_error_message && `: ${status.last_error_message}`}
        </p>
      )}

      {status.observed_at && (
        <p className="font-mono text-2xs text-muted-foreground">
          reported {formatRelative(status.observed_at)} · generation{" "}
          {status.observed_generation}
          {status.observed_generation !== deployment.generation &&
            ` (declared ${deployment.generation})`}
        </p>
      )}
    </div>
  )
}
