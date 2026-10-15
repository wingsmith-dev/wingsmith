import { StatusDot } from "@/components/layout/status-dot"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { formatAbsolute, formatRelative } from "@/lib/format"
import { humanize, syncTone } from "@/modules/clusters/display"
import type { ClusterSync } from "@/modules/clusters/types"

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5">
      <span className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
        {label}
      </span>
      <span className="truncate font-mono text-xs">{value}</span>
    </div>
  )
}

/**
 * The observed half of a cluster. `state` is derived by the plane — a cluster
 * that has never connected is not the same as one that connected and dropped,
 * and neither is a value anybody declared.
 */
export function SyncCard({ sync }: { sync: ClusterSync }) {
  return (
    <Card size="sm">
      <CardHeader className="pb-1">
        <CardTitle className="flex items-center gap-2 text-sm">
          <StatusDot
            tone={syncTone(sync.state)}
            pulse={sync.state === "connected"}
          />
          Sync connection
        </CardTitle>
      </CardHeader>
      <CardContent className="divide-y">
        <Row label="State" value={humanize(sync.state)} />
        <Row label="Operator" value={sync.operator_version ?? "—"} />
        <Row
          label="Multi-node"
          value={sync.multinode ? "Supported" : "No backend"}
        />
        {sync.read_only && (
          <div className="py-2">
            <p className="font-mono text-2xs tracking-wider text-warning uppercase">
              Read-only operator
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              This operator reports what the cluster has and applies nothing the
              plane declares. Anything placed here stays at zero replicas until
              the cluster is upgraded with <code>cluster.readOnly: false</code>.
            </p>
          </div>
        )}
        <Row
          label="Last sync"
          value={
            <span title={formatAbsolute(sync.last_sync_at)}>
              {formatRelative(sync.last_sync_at)}
            </span>
          }
        />
        <Row
          label="Last seen"
          value={
            <span title={formatAbsolute(sync.last_seen_at)}>
              {formatRelative(sync.last_seen_at)}
            </span>
          }
        />
        <Row
          label="Stream lag"
          value={sync.lag_ms === null ? "—" : `${sync.lag_ms} ms`}
        />
        {sync.last_error && (
          <div className="pt-2">
            <p className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
              Last error
            </p>
            <p className="mt-1 font-mono text-xs break-words text-destructive">
              {sync.last_error}
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
