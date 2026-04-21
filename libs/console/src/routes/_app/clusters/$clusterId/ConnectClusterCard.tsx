import { CopyableBlock } from "@/components/layout/mono"
import { StatusDot } from "@/components/layout/status-dot"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { formatError } from "@/lib/format"
import { cn } from "@/lib/utils"
import { useClusterInstall } from "@/modules/clusters/queries"
import type { Cluster } from "@/modules/clusters/types"
import { IconCheck } from "@tabler/icons-react"

/**
 * The bridge between a declared cluster and a running operator. `POST /clusters`
 * only writes the registry row; a cluster does nothing until an operator is
 * installed in it, dials the hub, and starts reporting. This panel walks that
 * gap — a checklist driven by observed state, and a minted one-liner that
 * installs the operator against whatever cluster kubectl is pointed at.
 *
 * It is deliberately shown only while onboarding is unfinished. Once the agent
 * is connected and a serving address is set, {@link SyncCard} and
 * {@link ServingCard} carry the live state and this panel steps aside.
 */
export function ConnectClusterCard({ cluster }: { cluster: Cluster }) {
  const connected = cluster.sync.state !== "never_connected"
  const serving = cluster.serving !== null
  const install = useClusterInstall()

  const steps = [
    {
      label: "Declared in the plane",
      done: true,
      note: "The registry row exists. Frames from an unknown cluster are rejected, so this comes first — and it already has.",
    },
    {
      label: "Operator connected",
      done: connected,
      note: "Install the operator in the cluster (below). It dials the hub outbound and the phase flips to active on the first handshake.",
    },
    {
      label: "Serving address set",
      done: serving,
      note: "Declare where the gateway reaches this cluster's runtime. Until then it is schedulable but nothing can route inference to it.",
    },
  ]

  const minutes = install.data
    ? Math.round(install.data.expires_in_seconds / 60)
    : 0

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
          <div className="space-y-2">
            <p className="text-2xs leading-relaxed text-muted-foreground/60">
              Run this against the target cluster — wherever your{" "}
              <code>kubectl</code> is pointed. It fetches and applies the
              operator manifest; the enrolment credential is delivered inside
              it, never shown here.
            </p>
            {install.data ? (
              <>
                <CopyableBlock value={install.data.command} />
                <p className="text-2xs text-muted-foreground/40">
                  Expires in ~{minutes} min. Generate a fresh command if it
                  lapses.
                </p>
              </>
            ) : (
              <Button
                size="sm"
                variant="outline"
                disabled={install.isPending}
                onClick={() => install.mutate(cluster.id)}
              >
                {install.isPending ? "Generating…" : "Get install command"}
              </Button>
            )}
            {install.error && (
              <p className="text-2xs text-destructive">
                {formatError(install.error, "Could not generate a command")}
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
