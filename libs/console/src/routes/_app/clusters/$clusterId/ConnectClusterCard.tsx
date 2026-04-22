import { CopyableBlock } from "@/components/layout/mono"
import { StatusDot } from "@/components/layout/status-dot"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { formatError } from "@/lib/format"
import { cn } from "@/lib/utils"
import { useClusterSetup } from "@/modules/clusters/queries"
import type { Cluster } from "@/modules/clusters/types"
import { IconCheck } from "@tabler/icons-react"

/**
 * The bridge between a declared cluster and a running operator. `POST /clusters`
 * only writes the registry row; a cluster does nothing until the cluster-setup
 * chart is installed in it, the operator dials the hub, and it starts reporting.
 * This panel walks that gap — a checklist driven by observed state, and the
 * per-cluster `helm` install that makes it hangar-ready.
 *
 * It is deliberately shown only while onboarding is unfinished. Once the agent
 * is connected and a serving address is set, {@link SyncCard} and
 * {@link ServingCard} carry the live state and this panel steps aside.
 */
export function ConnectClusterCard({ cluster }: { cluster: Cluster }) {
  const connected = cluster.sync.state !== "never_connected"
  const serving = cluster.serving !== null
  const setup = useClusterSetup(connected ? "" : cluster.id)

  const steps = [
    {
      label: "Declared in the plane",
      done: true,
      note: "The registry row exists. Frames from an unknown cluster are rejected, so this comes first — and it already has.",
    },
    {
      label: "Operator connected",
      done: connected,
      note: "Install the cluster-setup chart (below). The operator dials the hub outbound and the phase flips to active on the first handshake.",
    },
    {
      label: "Serving address set",
      done: serving,
      note: "Declare where the gateway reaches this cluster's runtime. Until then it is schedulable but nothing can route inference to it.",
    },
  ]

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Connect this cluster</CardTitle>
      </CardHeader>
      <CardContent className="space-y-5">
        <ol className="space-y-3">
          {steps.map((step) => (
            <li key={step.label} className="flex gap-3">
              <span className="mt-0.5 flex size-4 shrink-0 items-center justify-center">
                {step.done ? (
                  <IconCheck className="size-4 text-emerald-500" />
                ) : (
                  <StatusDot tone="neutral" />
                )}
              </span>
              <div className="space-y-0.5">
                <p
                  className={cn(
                    "font-mono text-xs",
                    step.done && "text-muted-foreground line-through"
                  )}
                >
                  {step.label}
                </p>
                <p className="text-2xs leading-relaxed text-muted-foreground/60">
                  {step.note}
                </p>
              </div>
            </li>
          ))}
        </ol>

        {!connected && (
          <div className="space-y-4">
            <p className="text-2xs leading-relaxed text-muted-foreground/60">
              Run these against the target cluster — wherever your{" "}
              <code>kubectl</code> is pointed. Helm pulls the chart from its own
              registry, so this works even where the plane is unreachable.
            </p>

            {setup.isLoading && (
              <p className="text-2xs text-muted-foreground/40">
                Preparing setup…
              </p>
            )}

            {setup.data && (
              <>
                <div className="space-y-1.5">
                  <p className="text-2xs font-medium">
                    1. Create the secrets the chart references
                  </p>
                  <p className="text-2xs leading-relaxed text-muted-foreground/50">
                    The enrolment token and Hugging Face token are created out
                    of band, never rendered here.
                  </p>
                  {setup.data.secret_commands.map((cmd) => (
                    <CopyableBlock key={cmd} value={cmd} />
                  ))}
                </div>

                <div className="space-y-1.5">
                  <p className="text-2xs font-medium">
                    2. Save this as <code>values.yaml</code>
                  </p>
                  <p className="text-2xs leading-relaxed text-muted-foreground/50">
                    Cluster identity only. Merge it with a local{" "}
                    <code>values.local.yaml</code> holding this cluster's own
                    facts — pool labels, storage class, serving host.
                  </p>
                  <CopyableBlock value={setup.data.values_yaml} />
                </div>

                <div className="space-y-1.5">
                  <p className="text-2xs font-medium">3. Install</p>
                  {setup.data.registry_login && (
                    <>
                      <p className="text-2xs leading-relaxed text-muted-foreground/50">
                        The chart registry is private — log in first, with a
                        token that can pull it.
                      </p>
                      <CopyableBlock value={setup.data.registry_login} />
                    </>
                  )}
                  <CopyableBlock value={setup.data.helm_command} />
                </div>
              </>
            )}

            {setup.error && (
              <p className="text-2xs text-destructive">
                {formatError(setup.error, "Could not prepare setup")}
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
