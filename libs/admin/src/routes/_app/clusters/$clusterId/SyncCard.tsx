import { StatusDot } from "@/components/layout/status-dot"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { formatAbsolute, formatRelative } from "@/lib/format"
import { SYNC_LABEL, SYNC_TONE } from "@/modules/clusters/display"
import type { ClusterSync } from "@/modules/clusters/types"

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5">
      <span className="font-mono text-2xs uppercase tracking-wider text-muted-foreground/60">
        {label}
      </span>
      <span className="font-mono text-xs">{value}</span>
    </div>
  )
}

export function SyncCard({ sync }: { sync: ClusterSync }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm">
          <StatusDot
            tone={SYNC_TONE[sync.state]}
            pulse={sync.state === "connected"}
          />
          Sync connection
          <span className="ml-auto font-mono text-2xs font-normal text-muted-foreground/60">
            operator {sync.operator_version}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="divide-y">
        <Row label="State" value={SYNC_LABEL[sync.state]} />
        <Row
          label="Connected since"
          value={
            sync.connected_since ? formatRelative(sync.connected_since) : "—"
          }
        />
        <Row
          label="Last heartbeat"
          value={formatRelative(sync.last_heartbeat_at)}
        />
        <Row
          label="Last sync"
          value={
            <span title={formatAbsolute(sync.last_sync_at)}>
              {formatRelative(sync.last_sync_at)}
            </span>
          }
        />
        <Row
          label="Stream lag"
          value={sync.stream_lag_ms === null ? "—" : `${sync.stream_lag_ms} ms`}
        />
        <Row label="Pending declarations" value={sync.pending_declarations} />
        {sync.last_error && (
          <div className="pt-2">
            <p className="font-mono text-2xs uppercase tracking-wider text-muted-foreground/60">
              Last error
            </p>
            <p className="mt-1 break-words font-mono text-xs text-destructive">
              {sync.last_error}
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
