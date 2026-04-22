import { CopyableBlock } from "@/components/layout/mono"
import { StatusDot } from "@/components/layout/status-dot"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { formatError } from "@/lib/format"
import { cn } from "@/lib/utils"
import {
  useClusterSetup,
  useIssueClusterEnrolmentToken,
} from "@/modules/clusters/queries"
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
  const mint = useIssueClusterEnrolmentToken()

  // The mint response carries the token once and folds it into the values; prefer
  // it over the tokenless GET whenever we have just minted in this session.
  const view = mint.data ?? setup.data
  const freshToken = mint.data?.enrolment_token ?? null

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

            {view && (
              <>
                <div className="space-y-1.5">
                  <p className="text-2xs font-medium">1. Enrolment token</p>
                  {freshToken ? (
                    <>
                      <p className="text-2xs leading-relaxed text-amber-500">
                        Shown once — copy it now. It is folded into the
                        <code> values.yaml</code> below, so the chart creates the
                        operator secret; you won't see it again.
                      </p>
                      <CopyableBlock value={freshToken} />
                    </>
                  ) : view.enrolment_token_set ? (
                    <div className="space-y-1.5">
                      <p className="text-2xs leading-relaxed text-muted-foreground/50">
                        A token was generated for this cluster and shown once. If
                        you didn't capture it, rotate to mint a new one — the
                        previous token stops working immediately.
                      </p>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={mint.isPending}
                        onClick={() => mint.mutate(cluster.id)}
                      >
                        {mint.isPending ? "Rotating…" : "Rotate token"}
                      </Button>
                    </div>
                  ) : (
                    <div className="space-y-1.5">
                      <p className="text-2xs leading-relaxed text-muted-foreground/50">
                        Generate the per-cluster token the operator authenticates
                        with. It is shown once and baked into the values below.
                      </p>
                      <Button
                        size="sm"
                        disabled={mint.isPending}
                        onClick={() => mint.mutate(cluster.id)}
                      >
                        {mint.isPending
                          ? "Generating…"
                          : "Generate enrolment token"}
                      </Button>
                    </div>
                  )}
                  {mint.error && (
                    <p className="text-2xs text-destructive">
                      {formatError(mint.error, "Could not mint the token")}
                    </p>
                  )}
                </div>

                <div className="space-y-1.5">
                  <div className="flex items-center justify-between">
                    <p className="text-2xs font-medium">
                      2. Save as <code>values.yaml</code> and edit
                    </p>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-5 px-1.5 text-2xs"
                      onClick={() => downloadValues(view.values_yaml)}
                    >
                      Download
                    </Button>
                  </div>
                  <p className="text-2xs leading-relaxed text-muted-foreground/50">
                    The top block is filled in{" "}
                    {freshToken ? "— identity and enrolment token" : ""}. Fill in
                    the blanks below the divider with this cluster's own facts —
                    pool labels, the RWX storage class, and the HF token.
                  </p>
                  <CopyableBlock value={view.values_yaml} />
                </div>

                <div className="space-y-1.5">
                  <p className="text-2xs font-medium">3. Install</p>
                  {view.registry_login && (
                    <>
                      <p className="text-2xs leading-relaxed text-muted-foreground/50">
                        The chart registry is private — log in first, with a
                        token that can pull it.
                      </p>
                      <CopyableBlock value={view.registry_login} />
                    </>
                  )}
                  <CopyableBlock value={view.helm_command} />
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

/** Save the rendered values as a `values.yaml` file the operator edits and runs helm against. */
function downloadValues(yaml: string) {
  const url = URL.createObjectURL(new Blob([yaml], { type: "text/yaml" }))
  const a = document.createElement("a")
  a.href = url
  a.download = "values.yaml"
  a.click()
  URL.revokeObjectURL(url)
}
