import { Field, FieldGrid } from "@/components/layout/field"
import { CopyableId, Dash, Labels } from "@/components/layout/mono"
import { StatusLabel } from "@/components/layout/status-dot"
import { WriteAction } from "@/components/layout/write-action"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import {
  CLUSTER_PHASE_TONE,
  humanize,
  syncTone,
} from "@/modules/clusters/display"
import { useClearClusterServing } from "@/modules/clusters/queries"
import { CLUSTER_STAGE_META, clusterStage } from "@/modules/clusters/stage"
import type { Cluster } from "@/modules/clusters/types"
import { useState } from "react"
import { toast } from "sonner"
import { ServingForm } from "./ServingForm"

/**
 * The cluster's data-plane address, a section of the identity card instead of a
 * card of its own. Declared, and read by nothing in the control plane: the
 * gateway is the only thing that ever dials it, which is why a cluster with no
 * address here is still a perfectly schedulable cluster.
 */
function ServingSection({ cluster }: { cluster: Cluster }) {
  const [editing, setEditing] = useState(false)
  const clear = useClearClusterServing()
  const serving = cluster.serving

  return (
    <div className="space-y-3 border-t pt-4">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h3 className="font-heading text-sm font-medium">Serving</h3>
          {serving && (
            <StatusLabel tone={serving.enabled ? "ok" : "neutral"}>
              {serving.enabled ? "enabled" : "disabled"}
            </StatusLabel>
          )}
        </div>
        <WriteAction>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() => setEditing(true)}
            >
              {serving ? "Edit" : "Set address"}
            </Button>
            {serving && (
              <Button
                size="sm"
                variant="ghost"
                disabled={clear.isPending}
                onClick={() =>
                  clear.mutate(cluster.id, {
                    onSuccess: () => toast.success("Serving address cleared"),
                    onError: (error) =>
                      toast.error(
                        formatError(error, "Could not clear the address")
                      ),
                  })
                }
              >
                Clear
              </Button>
            )}
          </div>
        </WriteAction>
      </div>

      {serving ? (
        <FieldGrid>
          <Field label="URL" className="sm:col-span-2">
            <span className="break-all">{serving.url}</span>
          </Field>
          <Field label="Credential">
            <Dash>{serving.credential_id}</Dash>
          </Field>
        </FieldGrid>
      ) : (
        <p className="font-mono text-xs text-muted-foreground">
          No address. Nothing can route traffic to this cluster until one is
          declared.
        </p>
      )}

      {editing && (
        <ServingForm
          cluster={cluster}
          open={editing}
          onOpenChange={setEditing}
        />
      )}
    </div>
  )
}

/**
 * Identity, declared and observed in one card. Stage is the derived headline the
 * rest of the page is arranged around; phase and reachability sit apart from it
 * by design, because a cluster can be declared active and be unreachable at
 * once.
 */
export function ClusterIdentity({ cluster }: { cluster: Cluster }) {
  const stage = CLUSTER_STAGE_META[clusterStage(cluster)]
  return (
    <Card className="p-4">
      <FieldGrid>
        <Field label="Cluster">
          {cluster.name}
          <CopyableId
            value={cluster.id}
            className="ml-2 text-2xs text-muted-foreground"
          />
        </Field>
        <Field label="Region">
          {cluster.region}
          {cluster.zone && ` · ${cluster.zone}`}
        </Field>
        <Field label="Stage">
          <StatusLabel tone={stage.tone}>{stage.label}</StatusLabel>
        </Field>
        <Field label="Phase">
          <StatusLabel tone={CLUSTER_PHASE_TONE[cluster.phase] ?? "neutral"}>
            {humanize(cluster.phase)}
          </StatusLabel>
        </Field>
        <Field label="Reachability">
          <StatusLabel tone={cluster.reachable ? "ok" : "danger"}>
            {cluster.reachable ? "reachable" : "unreachable"}
          </StatusLabel>
        </Field>
        <Field label="Sync">
          <StatusLabel tone={syncTone(cluster.sync.state)}>
            {humanize(cluster.sync.state)}
          </StatusLabel>
        </Field>
        <Field label="Operator version">
          <Dash>{cluster.sync.operator_version}</Dash>
        </Field>
        <Field label="Last sync">
          <span title={formatAbsolute(cluster.sync.last_sync_at)}>
            {formatRelative(cluster.sync.last_sync_at)}
          </span>
        </Field>
        <Field label="Enrolled">
          {cluster.enrolled_at
            ? formatAbsolute(cluster.enrolled_at)
            : "not enrolled"}
        </Field>
        <Field label="Residency">
          <Dash>{cluster.residency}</Dash>
        </Field>
        <Field label="Metrics label">
          <Dash>{cluster.metrics_label}</Dash>
        </Field>
        <Field label="Capabilities">
          {Object.keys(cluster.capabilities).length === 0 ? (
            <span className="text-muted-foreground">none</span>
          ) : (
            Object.entries(cluster.capabilities)
              .map(([key, value]) =>
                value === true ? key : `${key}=${String(value)}`
              )
              .join(" · ")
          )}
        </Field>
        <Field label="Labels" className="sm:col-span-2">
          <Labels labels={cluster.labels} />
        </Field>
      </FieldGrid>

      <ServingSection cluster={cluster} />
    </Card>
  )
}
