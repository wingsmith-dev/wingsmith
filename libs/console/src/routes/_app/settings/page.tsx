import { Field, FieldGrid } from "@/components/layout/field"
import { PageHeader } from "@/components/layout/page-header"
import { ResourceIcon } from "@/components/layout/resource-icon"
import { StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { useTheme } from "@/components/theme-provider"
import { BASE_URL } from "@/lib/api/http"
import { transport } from "@/lib/api"
import { useAuthStore } from "@/modules/auth/store"
import { useSignOut } from "@/modules/auth/session"
import { useSettingsStore } from "@/modules/settings/store"
import { usePolicies, usePriorityClasses } from "@/modules/binding/queries"
import type { Policy, PriorityClass } from "@/modules/binding/types"
import { useClusterOptions } from "@/modules/clusters/queries"
import { usePlaneConfig } from "@/modules/system/queries"
import { sourceRows, unmappedCount } from "@/modules/system/sources"
import type { SourceRow } from "@/modules/system/sources"
import type { PlaneConfig } from "@/modules/system/types"
import { gibibytes } from "@/modules/supply/display"
import { useDeviceClasses } from "@/modules/supply/queries"
import type { DeviceClass } from "@/modules/supply/types"
import {
  IconArrowUpRight,
  IconDeviceDesktopAnalytics,
  IconEye,
  IconServerCog,
  IconSettings,
  IconShieldLock,
  IconSortDescending,
} from "@tabler/icons-react"
import { Link, useSearch } from "@tanstack/react-router"
import type { SettingsSection } from "./route"

/**
 * Three kinds of thing live here. **Definitions** are reference data — hardware
 * types, scheduling classes and guardrails the rest of the console
 * points at; they are the plane's, read constantly and written rarely. **Plane**
 * is the running process described back to you: nothing under it is editable
 * from anywhere, which is why it is not filed with the definitions it sits
 * beside. Beneath both, **Console** settings belong to this browser and are
 * never written over the API, so two operators can hold different views of the
 * same fleet.
 *
 * One section is on screen at a time and the nav picks it, held in `?section=`
 * so a view is linkable and the back button walks it.
 */
export function SettingsPage() {
  const { section } = useSearch({ from: "/_app/settings" })
  const Section = SECTIONS[section]

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Settings"
        description="definitions · plane · this console"
      />

      <div className="grid gap-6 p-4 md:grid-cols-[12rem_minmax(0,1fr)]">
        <SettingsNav current={section} />

        <div className="flex min-w-0 flex-col gap-4">
          <Section />
        </div>
      </div>
    </div>
  )
}

const SECTIONS: Record<SettingsSection, React.ComponentType> = {
  "gpu-models": GpuModelsSection,
  priority: PriorityClassesSection,
  policies: PoliciesSection,
  configuration: ConfigurationSection,
  preferences: PreferencesSection,
  access: AccessSection,
}

const NAV_ITEMS = [
  { label: "Definitions", kind: "label" as const },
  {
    section: "gpu-models" as const,
    label: "GPU models",
    icon: IconDeviceDesktopAnalytics,
  },
  {
    section: "priority" as const,
    label: "Priority classes",
    icon: IconSortDescending,
  },
  { section: "policies" as const, label: "Policies", icon: IconShieldLock },
  { label: "Plane", kind: "label" as const },
  {
    section: "configuration" as const,
    label: "Configuration",
    icon: IconServerCog,
  },
  { label: "Console", kind: "label" as const },
  {
    section: "preferences" as const,
    label: "Preferences",
    icon: IconSettings,
  },
  { section: "access" as const, label: "Access & tokens", icon: IconEye },
]

function SettingsNav({ current }: { current: SettingsSection }) {
  return (
    <nav className="top-20 flex flex-row flex-wrap gap-0.5 self-start md:sticky md:flex-col">
      {NAV_ITEMS.map((item) =>
        "kind" in item ? (
          <span
            key={item.label}
            className="flex h-8 items-center px-2 font-mono text-2xs tracking-widest text-muted-foreground/50 uppercase"
          >
            {item.label}
          </span>
        ) : (
          <Link
            key={item.section}
            to="/settings"
            search={{ section: item.section }}
            aria-current={current === item.section ? "page" : undefined}
            className={
              current === item.section
                ? "flex h-8 items-center gap-2 rounded-md bg-primary/10 px-2 text-xs font-medium text-primary"
                : "flex h-8 items-center gap-2 rounded-md px-2 text-xs text-muted-foreground hover:bg-accent hover:text-foreground"
            }
          >
            <item.icon className="size-4 shrink-0" />
            {item.label}
          </Link>
        )
      )}
    </nav>
  )
}

function Panel({
  id,
  title,
  description,
  action,
  children,
}: React.PropsWithChildren<{
  id: string
  title: string
  description: string
  action?: React.ReactNode
}>) {
  return (
    <Card id={id} className="scroll-mt-20 gap-0 overflow-hidden p-0">
      <div className="flex items-start justify-between gap-3 border-b bg-muted/30 px-4 py-3">
        <div>
          <h2 className="text-sm leading-none font-semibold">{title}</h2>
          <p className="mt-1 font-mono text-2xs text-muted-foreground/60">
            {description}
          </p>
        </div>
        {action}
      </div>
      <div className="p-4">{children}</div>
    </Card>
  )
}

/**
 * The shared device-class dictionary, summarised. The full catalog — with the
 * fleet-inventory joins, filters and the unnamed-class alarm — is its own
 * surface at `/device-classes`, reached from the header here.
 */
function GpuModelsSection() {
  const { data, isLoading } = useDeviceClasses()
  const classes = data ?? []

  return (
    <Panel
      id="gpu-models"
      title="GPU models"
      description="hardware types Hangar knows how to schedule"
      action={
        <Link
          to="/device-classes"
          className="inline-flex items-center gap-1 font-mono text-2xs text-primary hover:underline"
        >
          Full catalog
          <IconArrowUpRight className="size-3.5" />
        </Link>
      }
    >
      {isLoading ? (
        <Skeleton className="h-48 w-full" />
      ) : classes.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">
          No device classes in the catalog yet.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Model</TableHead>
                <TableHead>Memory</TableHead>
                <TableHead>Architecture</TableHead>
                <TableHead className="hidden lg:table-cell">Form</TableHead>
                <TableHead>In use</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {classes.map((entry) => (
                <GpuModelRow key={entry.name} entry={entry} />
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </Panel>
  )
}

function GpuModelRow({ entry }: { entry: DeviceClass }) {
  const clusters = entry.inventory.clusters.length
  const devices = entry.inventory.devices
  return (
    <TableRow>
      <TableCell>
        <div className="flex items-start gap-2">
          <ResourceIcon
            kind="device-class"
            className="mt-0.5 size-3.5 shrink-0 text-muted-foreground"
          />
          <div className="min-w-0">
            <Link
              to="/devices"
              search={{ class: entry.name }}
              className="block hover:text-primary hover:underline"
            >
              <span className="inline-flex items-center gap-1.5 text-xs">
                {entry.label}
                <IconArrowUpRight className="size-3 text-muted-foreground" />
              </span>
              <span className="block truncate font-mono text-2xs text-muted-foreground/60">
                {entry.name}
              </span>
            </Link>
            {!entry.in_catalog && (
              <span className="font-mono text-2xs text-amber-600 dark:text-amber-400">
                not in the catalog
              </span>
            )}
          </div>
        </div>
      </TableCell>
      <TableCell className="font-mono text-2xs tabular-nums">
        {entry.memory_mib ? gibibytes(entry.memory_mib) : "—"}
      </TableCell>
      <TableCell className="font-mono text-2xs text-muted-foreground">
        {entry.architecture || "—"}
      </TableCell>
      <TableCell className="hidden font-mono text-2xs text-muted-foreground lg:table-cell">
        {entry.form_factor || "—"}
      </TableCell>
      <TableCell className="font-mono text-2xs">
        {devices > 0 ? (
          <span>
            {clusters} {clusters === 1 ? "cluster" : "clusters"} · {devices}
          </span>
        ) : (
          <span className="text-muted-foreground/50">not in use</span>
        )}
      </TableCell>
    </TableRow>
  )
}

/**
 * The plane's scheduling classes, summarised. The full surface — with create,
 * edit and the total-ordering guard — is its own page at `/priority-classes`,
 * reached from the header here.
 */
function PriorityClassesSection() {
  const list = usePriorityClasses()
  const classes = (list.items ?? []).slice().sort((a, b) => b.value - a.value)

  return (
    <Panel
      id="priority"
      title="Priority classes"
      description="scheduling order and preemption behaviour"
      action={
        <Link
          to="/priority-classes"
          className="inline-flex items-center gap-1 font-mono text-2xs text-primary hover:underline"
        >
          Manage
          <IconArrowUpRight className="size-3.5" />
        </Link>
      }
    >
      {list.isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : classes.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">
          No priority classes defined yet.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Class</TableHead>
                <TableHead className="text-right">Value</TableHead>
                <TableHead>Description</TableHead>
                <TableHead>Preemptible</TableHead>
                <TableHead>Preempts</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {classes.map((c) => (
                <PriorityClassRow key={c.id} entry={c} />
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      <p className="mt-3 text-xs text-muted-foreground">
        Priority decides who wins when quota is contended — consumption itself
        is tracked under Access › Quotas.
      </p>
    </Panel>
  )
}

function PriorityClassRow({ entry }: { entry: PriorityClass }) {
  return (
    <TableRow>
      <TableCell className="font-mono text-2xs">
        {entry.name}
        {entry.is_default && (
          <Badge variant="outline" className="ml-2 font-mono text-2xs">
            default
          </Badge>
        )}
      </TableCell>
      <TableCell className="text-right font-mono text-2xs tabular-nums">
        {entry.value}
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {entry.description || "—"}
      </TableCell>
      <TableCell className="font-mono text-2xs text-muted-foreground">
        {entry.preemptible ? "yes" : "no"}
      </TableCell>
      <TableCell className="font-mono text-2xs text-muted-foreground">
        {entry.preempts ? "yes" : "no"}
      </TableCell>
    </TableRow>
  )
}

/**
 * The plane's placement guardrails, summarised. The full surface — with the
 * allow/deny lists, rules and subject attachments — is its own page at
 * `/policies`, reached from the header here.
 */
function PoliciesSection() {
  const list = usePolicies()
  const policies = list.items ?? []

  return (
    <Panel
      id="policies"
      title="Policies"
      description="guardrails applied at admission and scheduling time"
      action={
        <Link
          to="/policies"
          className="inline-flex items-center gap-1 font-mono text-2xs text-primary hover:underline"
        >
          Manage
          <IconArrowUpRight className="size-3.5" />
        </Link>
      }
    >
      {list.isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : policies.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">
          No policies defined yet.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Policy</TableHead>
                <TableHead>Description</TableHead>
                <TableHead>Mode</TableHead>
                <TableHead>Scope</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {policies.map((p) => (
                <PolicyRow key={p.id} entry={p} />
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </Panel>
  )
}

function PolicyRow({ entry }: { entry: Policy }) {
  const scope =
    entry.attachments.length === 0
      ? "unattached"
      : entry.attachments
          .map((a) => a.subject_name || a.subject || a.subject_id)
          .join(", ")

  return (
    <TableRow>
      <TableCell className="font-mono text-2xs">{entry.name}</TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {entry.description || "—"}
      </TableCell>
      <TableCell>
        <StatusLabel tone={entry.enabled ? "ok" : "neutral"}>
          {entry.enabled ? "Enforcing" : "Disabled"}
        </StatusLabel>
      </TableCell>
      <TableCell className="font-mono text-2xs text-muted-foreground">
        {scope}
      </TableCell>
    </TableRow>
  )
}

/**
 * What the plane is actually running with. Read-only, and not a definition: no
 * row here can be changed from the console, or over the API at all — the values
 * come from the process's environment and change only when it is redeployed.
 *
 * Metrics sources lead because they are the reason this section exists. A
 * mis-set `HANGAR_METRICS_SOURCES` silences a cluster's panels without failing
 * anything, and finding that out used to mean reading a ConfigMap in the
 * cluster. Everything below it is the same class of question — which database,
 * which hub URL, which operator image — answered before somebody has to guess.
 *
 * Credentials are absent by construction. The plane exposes an allowlist and
 * reduces every token, password and signing key to a boolean, so there is
 * nothing on this screen that must not be in a screenshot.
 */
function ConfigurationSection() {
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
  return <span className="text-muted-foreground/50">not set</span>
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
            no metrics source. The panels come back empty rather than wrong,
            which is intended — but it is not a healthy fleet, and it is fixed
            by adding an entry to
            <span className="mx-1 font-mono text-2xs">
              HANGAR_METRICS_SOURCES
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
              <span className="text-muted-foreground/50">dedicated store</span>
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

        <p className="font-mono text-2xs text-muted-foreground/60">
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
              <span className="text-muted-foreground/50">
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
 * Whether a credential exists, never what it is. The plane sends booleans here
 * — not a prefix, not a length, not a digest — so this panel cannot be made to
 * narrow a search for the real value.
 */
function AuthPanel({ config }: { config: PlaneConfig }) {
  const { auth } = config
  const enabled = auth.admin_auth === "enabled"

  return (
    <Panel
      id="auth"
      title="Authentication"
      description="which credentials are configured — never their values"
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
            <span className="text-muted-foreground/50">latest</span>
          )}
        </Field>
        <Field label="Operator image">
          {setup.operator_image || (
            <span className="text-muted-foreground/50">
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

/** This browser's own settings — safety, what it shows, and appearance. */
/**
 * Four panels rather than one: read-only is a safety switch, economics is who
 * is allowed to see the money, appearance is taste, and the plane endpoint is a
 * build-time fact nobody can change from here. Reading them as one list invites
 * the last to be mistaken for a setting.
 */
function PreferencesSection() {
  return (
    <>
      <ReadOnlyPanel />
      <EconomicsPanel />
      <AppearancePanel />
      <PlanePanel />
    </>
  )
}

function EconomicsPanel() {
  const economics = useSettingsStore((s) => s.economics)
  const setEconomics = useSettingsStore((s) => s.setEconomics)

  return (
    <Panel
      id="economics"
      title="Unit economics"
      description="what a workload costs and what it earns"
    >
      <div className="space-y-4">
        <div className="flex items-start justify-between gap-4 rounded-md border px-3 py-2.5">
          <div className="space-y-0.5">
            <Label htmlFor="economics">Show unit economics</Label>
            <p className="text-2xs text-muted-foreground/70">
              Adds an Economics view to every workload — prices, cost per hour,
              margin over the day's demand curve, and break-even.
            </p>
          </div>
          <Switch
            id="economics"
            checked={economics}
            onCheckedChange={setEconomics}
          />
        </div>

        <p className="rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          Off unless <code className="font-mono">VITE_ECONOMICS</code> is set,
          so a console handed to a customer shows no tab at all. Every figure in
          that view is a model built in this browser from what you type; the
          plane holds no price and no cost, and nothing there is ever written
          back to it.
        </p>
      </div>
    </Panel>
  )
}

function ReadOnlyPanel() {
  const readOnly = useSettingsStore((s) => s.readOnly)
  const setReadOnly = useSettingsStore((s) => s.setReadOnly)

  return (
    <Panel
      id="preferences"
      title="Read-only mode"
      description="what this console is allowed to write"
    >
      <div className="space-y-4">
        <div className="flex items-start justify-between gap-4 rounded-md border px-3 py-2.5">
          <div className="space-y-0.5">
            <Label htmlFor="read-only">Read-only mode</Label>
            <p className="text-2xs text-muted-foreground/70">
              Refuses every write before it leaves the browser, and hides the
              affordances that would start one.
            </p>
          </div>
          <Switch
            id="read-only"
            checked={readOnly}
            onCheckedChange={setReadOnly}
          />
        </div>

        <p className="rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          This stops <em>this console</em> writing. It does not stop anyone
          else, and it does not stop the hub reconciling a cluster toward
          declared state. Connecting a live cluster safely is a separate,
          per-cluster decision.
        </p>
      </div>
    </Panel>
  )
}

function AppearancePanel() {
  const { theme, setTheme } = useTheme()

  return (
    <Panel
      id="appearance"
      title="Appearance"
      description="held in this browser, never written to the plane"
    >
      <div className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="theme">Theme</Label>
          <Select
            value={theme}
            onValueChange={(v) => setTheme(v as typeof theme)}
          >
            <SelectTrigger id="theme" className="w-56 font-mono text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {["system", "light", "dark"].map((value) => (
                <SelectItem
                  key={value}
                  value={value}
                  className="font-mono text-xs"
                >
                  {value}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
    </Panel>
  )
}

/**
 * An empty {@link BASE_URL} is the plane image's deliberate setting, not a
 * missing one — every request goes to the origin that served the console. Naming
 * that origin is the only way the field can be read as configured rather than
 * blank.
 */
function PlanePanel() {
  const origin = typeof window === "undefined" ? null : window.location.origin

  return (
    <Panel
      id="plane"
      title="Plane"
      description="where this console sends its requests"
    >
      <div className="space-y-1.5">
        <FieldGrid>
          <Field label="Base URL">
            {BASE_URL || (
              <span>
                same origin
                {origin && (
                  <span className="ml-1.5 text-muted-foreground/60">
                    {origin}
                  </span>
                )}
              </span>
            )}
          </Field>
          <Field label="Transport">
            <Badge variant="outline" className="font-mono text-2xs">
              {transport.kind}
            </Badge>
          </Field>
        </FieldGrid>
        <p className="font-mono text-2xs text-muted-foreground/60">
          Set with VITE_API_BASE_URL and VITE_API_MODE at build time.
        </p>
      </div>
    </Panel>
  )
}

/** Who the plane thinks you are, and the way out. */
function AccessSection() {
  const operator = useAuthStore((s) => s.operator)
  const signOut = useSignOut()

  return (
    <Panel
      id="access"
      title="Access & tokens"
      description="who the plane thinks you are"
    >
      <div className="space-y-4">
        <FieldGrid>
          <Field label="Name">{operator?.name ?? "—"}</Field>
          <Field label="Email">{operator?.email ?? "—"}</Field>
        </FieldGrid>
        <button
          onClick={signOut}
          className="text-xs text-destructive hover:underline"
        >
          Sign out
        </button>
      </div>
    </Panel>
  )
}
