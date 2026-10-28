import { Panel } from "@/components/layout/panel"
import { Field, FieldGrid } from "@/components/layout/field"
import { StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { useClusterOptions } from "@/modules/clusters/queries"
import { usePlaneConfig } from "@/modules/system/queries"
import { sourceRows, unmappedCount } from "@/modules/system/sources"
import type { SourceRow } from "@/modules/system/sources"
import type { PlaneConfig } from "@/modules/system/types"

/**
 * What the plane is actually running with. Read-only, and not a definition: no
 * row here can be changed from the console, or over the API at all: the values
 * come from the process's environment and change only when it is redeployed.
 *
 * Metrics sources lead because they are the reason this section exists. A
 * mis-set `WINGSMITH_METRICS_SOURCES` silences a cluster's panels without failing
 * anything, and finding that out otherwise means reading a ConfigMap in the
 * cluster. Everything below it is the same class of question (which database,
 * which hub URL, which operator image), answered before somebody has to guess.
 *
 * Credentials are absent by construction. The plane exposes an allowlist and
 * reduces every token, password and signing key to a boolean, so there is
 * nothing on this screen that must not be in a screenshot.
 */
export function ConfigurationSection() {
  const { data, isLoading, isError } = usePlaneConfig()

  if (isLoading) {
    return (
      <Panel
        id="configuration"
        title="Configuration"
        description="what this plane is running with"
      >
        <Skeleton className="h-48 w-full" />
      </Panel>
    )
  }

  if (isError || !data) {
    return (
      <Panel
        id="configuration"
        title="Configuration"
        description="what this plane is running with"
      >
        <p className="py-6 text-center text-sm text-muted-foreground">
          The plane did not return its configuration. An older plane has no
          <span className="mx-1 font-mono text-2xs">/v1/system/config</span>
          endpoint.
        </p>
      </Panel>
    )
  }

  return (
    <>
      <MetricsSourcesPanel config={data} />
      <ProcessPanel config={data} />
      <HubPanel config={data} />
      <AuthPanel config={data} />
      <ClusterSetupPanel config={data} />
    </>
  )
}

/** A `Field` value that is configured nowhere. Blank would read as a value. */
function Unset() {
  return <span className="text-muted-foreground">not set</span>
}

function seconds(value: number) {
  return `${value}s`
}

function MetricsSourcesPanel({ config }: { config: PlaneConfig }) {
  const clusters = useClusterOptions()
  const { metrics } = config
  const rows = sourceRows(
    metrics.sources,
    clusters.data ?? [],
    metrics.shared_url
  )
  const unmapped = unmappedCount(rows)

  return (
    <Panel
      id="configuration"
      title="Metrics sources"
      description="which store answers for which cluster, and under which label"
    >
      <div className="space-y-3">
        <FieldGrid>
          <Field label="Shared store">{metrics.shared_url || <Unset />}</Field>
          <Field label="Window · step · timeout">
            {metrics.window} · {metrics.step} · {seconds(metrics.timeout)}
          </Field>
          <Field label="Fleet writes to">
            {metrics.remote_write_url || <Unset />}
          </Field>
          <Field label="Write auth htpasswd">
            {metrics.remote_write_auth_secret || <Unset />}
          </Field>
        </FieldGrid>

        {clusters.isLoading ? (
          <Skeleton className="h-32 w-full" />
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Cluster</TableHead>
                  <TableHead>Store</TableHead>
                  <TableHead>Kind</TableHead>
                  <TableHead>Cluster label</TableHead>
                  <TableHead>State</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row) => (
                  <SourceRowCells key={row.key} row={row} />
                ))}
              </TableBody>
            </Table>
          </div>
        )}

        {unmapped > 0 && (
          <p className="rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
            {unmapped === 1 ? "One cluster has" : `${unmapped} clusters have`}{" "}
            no metrics source. The panels come back empty instead of wrong,
            which is intended, but it is not a healthy fleet, and it is fixed by
            adding an entry to
            <span className="mx-1 font-mono text-2xs">
              WINGSMITH_METRICS_SOURCES
            </span>
            {metrics.shared_url
              ? "naming the cluster label its series carry."
              : "along with a default= endpoint, since no shared store is configured at all."}
          </p>
        )}
      </div>
    </Panel>
  )
}

function SourceRowCells({ row }: { row: SourceRow }) {
  return (
    <TableRow>
      <TableCell className="font-mono text-2xs">
        {row.key}
        {!row.known && (
          <Badge variant="outline" className="ml-2 font-mono text-2xs">
            no such cluster
          </Badge>
        )}
      </TableCell>
      <TableCell className="font-mono text-2xs text-muted-foreground">
        {row.source?.url ?? "—"}
      </TableCell>
      <TableCell className="font-mono text-2xs">
        {row.source ? (
          <Badge variant="outline" className="font-mono text-2xs">
            {row.source.kind}
          </Badge>
        ) : (
          "—"
        )}
      </TableCell>
      <TableCell className="font-mono text-2xs text-muted-foreground">
        {row.source
          ? row.source.label || (
              <span className="text-muted-foreground">dedicated store</span>
            )
          : "—"}
      </TableCell>
      <TableCell>
        {!row.source ? (
          <StatusLabel tone="warn">No source</StatusLabel>
        ) : !row.known ? (
          <StatusLabel tone="warn">Unmatched entry</StatusLabel>
        ) : (
          <StatusLabel tone="ok">Mapped</StatusLabel>
        )}
      </TableCell>
    </TableRow>
  )
}

function ProcessPanel({ config }: { config: PlaneConfig }) {
  const { app, api, database } = config

  return (
    <Panel
      id="process"
      title="Process"
      description="environment, logging and the database it is pointed at"
    >
      <div className="space-y-4">
        <FieldGrid>
          <Field label="Environment">
            <Badge variant="outline" className="font-mono text-2xs">
              {app.env}
            </Badge>
          </Field>
          <Field label="Debug">
            <StatusLabel tone={app.debug ? "warn" : "neutral"}>
              {app.debug ? "On" : "Off"}
            </StatusLabel>
          </Field>
          <Field label="Log level">
            <Badge variant="outline" className="font-mono text-2xs">
              {app.log_level}
            </Badge>
          </Field>
          <Field label="Log format">
            <Badge variant="outline" className="font-mono text-2xs">
              {app.log_format}
            </Badge>
          </Field>
          <Field label="API bind">
            {api.host}:{api.port}
          </Field>
          <Field label="Allowed origins">
            {app.allowed_origins.length === 0 ? (
              <Unset />
            ) : (
              app.allowed_origins.join(", ")
            )}
          </Field>
        </FieldGrid>

        <FieldGrid>
          <Field label="Database">
            {database.host || <Unset />}
            {database.port != null && `:${database.port}`}
            {database.database && ` / ${database.database}`}
          </Field>
          <Field label="Driver">
            <Badge variant="outline" className="font-mono text-2xs">
              {database.driver}
            </Badge>
          </Field>
          <Field label="Pool">
            {database.pool_size} + {database.pool_max_overflow} overflow
          </Field>
          <Field label="Pre-ping · echo">
            {database.pool_pre_ping ? "on" : "off"} ·{" "}
            {database.echo ? "on" : "off"}
          </Field>
        </FieldGrid>

        <p className="font-mono text-2xs text-muted-foreground">
          The connection is reported in parts. The plane never sends its
          DATABASE_URL, so no credential can travel with it.
        </p>
      </div>
    </Panel>
  )
}

function HubPanel({ config }: { config: PlaneConfig }) {
  const { hub, reactor } = config

  return (
    <Panel
      id="hub"
      title="Hub & reactor"
      description="where cluster agents dial, and how often conditions are re-read"
    >
      <div className="space-y-4">
        <FieldGrid>
          <Field label="Public URL">{hub.public_url || <Unset />}</Field>
          <Field label="Public host">{hub.public_host || <Unset />}</Field>
          <Field label="Bind">
            {hub.host}:{hub.port}
          </Field>
          <Field label="Path">{hub.path}</Field>
          <Field label="Instance id">
            {hub.instance_id || (
              <span className="text-muted-foreground">
                resolved per process
              </span>
            )}
          </Field>
          <Field label="Session TTL">{seconds(hub.session_ttl_seconds)}</Field>
          <Field label="Reaper interval">
            {seconds(hub.reaper_interval_seconds)}
          </Field>
          <Field label="Ownership check">
            {seconds(hub.ownership_check_seconds)}
          </Field>
        </FieldGrid>

        <FieldGrid>
          <Field label="Reactor">
            <StatusLabel tone={reactor.enabled ? "ok" : "neutral"}>
              {reactor.enabled ? "Running in the hub" : "Disabled"}
            </StatusLabel>
          </Field>
          <Field label="Tick · lease">
            {seconds(reactor.interval_seconds)} ·{" "}
            {seconds(reactor.lease_seconds)}
          </Field>
          <Field label="Event batch">{reactor.event_batch}</Field>
          <Field label="Notify timeout">
            {seconds(reactor.notify_timeout)}
          </Field>
        </FieldGrid>
      </div>
    </Panel>
  )
}

/**
 * Whether a credential exists, never what it is. The plane sends booleans here,
 * not a prefix, a length or a digest, so this panel cannot be made to narrow a
 * search for the real value.
 */
function AuthPanel({ config }: { config: PlaneConfig }) {
  const { auth } = config
  const enabled = auth.admin_auth === "enabled"

  return (
    <Panel
      id="auth"
      title="Authentication"
      description="which credentials are configured, never their values"
    >
      <div className="space-y-4">
        <FieldGrid>
          <Field label="Admin auth">
            <StatusLabel tone={enabled ? "ok" : "danger"}>
              {enabled ? "Enabled" : "Disabled"}
            </StatusLabel>
          </Field>
          <Field label="Session TTL">{seconds(auth.session_ttl_seconds)}</Field>
          <Field label="Admin password hash">
            <StatusLabel tone={auth.admin_password_hash_set ? "ok" : "neutral"}>
              {auth.admin_password_hash_set ? "Set" : "Not set"}
            </StatusLabel>
          </Field>
          <Field label="Session signing secret">
            <StatusLabel
              tone={auth.session_signing_secret_set ? "ok" : "neutral"}
            >
              {auth.session_signing_secret_set ? "Set" : "Not set"}
            </StatusLabel>
          </Field>
          <Field label="Fleet operator token">
            <StatusLabel tone={auth.operator_token_set ? "ok" : "neutral"}>
              {auth.operator_token_set ? "Set" : "Not set"}
            </StatusLabel>
          </Field>
          <Field label="HuggingFace token">
            <StatusLabel tone={auth.huggingface_token_set ? "ok" : "neutral"}>
              {auth.huggingface_token_set ? "Set" : "Not set"}
            </StatusLabel>
          </Field>
        </FieldGrid>

        {!enabled && (
          <p className="rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
            Admin authentication is off: every request to this API resolves to a
            full-scope admin with no credential check. Safe only behind a
            perimeter with no ingress.
          </p>
        )}
      </div>
    </Panel>
  )
}

function ClusterSetupPanel({ config }: { config: PlaneConfig }) {
  const { setup } = config

  return (
    <Panel
      id="setup"
      title="Cluster setup"
      description="what the register-cluster screen hands an operator to install"
    >
      <FieldGrid>
        <Field label="Chart ref">{setup.chart_ref || <Unset />}</Field>
        <Field label="Chart version">
          {setup.chart_version || (
            <span className="text-muted-foreground">latest</span>
          )}
        </Field>
        <Field label="Operator image">
          {setup.operator_image || (
            <span className="text-muted-foreground">
              chart&rsquo;s appVersion
            </span>
          )}
        </Field>
        <Field label="Registry">
          <Badge variant="outline" className="font-mono text-2xs">
            {setup.chart_private ? "private" : "public"}
          </Badge>
        </Field>
      </FieldGrid>
    </Panel>
  )
}
