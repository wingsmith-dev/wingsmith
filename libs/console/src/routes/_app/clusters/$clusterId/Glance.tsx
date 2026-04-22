import { Field, FieldGrid } from "@/components/layout/field"
import { CopyableId, Dash, Labels } from "@/components/layout/mono"
import { StatusLabel } from "@/components/layout/status-dot"
import { Card } from "@/components/ui/card"
import { formatAbsolute, formatRelative } from "@/lib/format"
import {
  CLUSTER_PHASE_TONE,
  humanize,
  syncTone,
} from "@/modules/clusters/display"
import type { Cluster } from "@/modules/clusters/types"

/**
 * Identity, declared and observed in one card. Phase and reachability sit apart
 * on purpose: a cluster can be declared active and be unreachable at once.
 */
export function ClusterIdentity({ cluster }: { cluster: Cluster }) {
  return (
    <Card className="p-4">
      <FieldGrid>
        <Field label="Cluster">
          {cluster.name}
          <CopyableId
            value={cluster.id}
            className="ml-2 text-2xs text-muted-foreground/60"
          />
        </Field>
        <Field label="Region">
          {cluster.region}
          {cluster.zone && ` · ${cluster.zone}`}
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
        <Field label="Agent version">
          <Dash>{cluster.sync.agent_version}</Dash>
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
        <Field label="Capabilities">
          {Object.keys(cluster.capabilities).length === 0 ? (
            <span className="text-muted-foreground/40">none</span>
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
    </Card>
  )
}
