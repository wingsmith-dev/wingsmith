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
import {
  ACCENTS,
  useSettingsStore,
  type Accent,
} from "@/modules/settings/store"
import { usePolicies, usePriorityClasses } from "@/modules/binding/queries"
import type { Policy, PriorityClass } from "@/modules/binding/types"
import { gibibytes } from "@/modules/supply/display"
import { useDeviceClasses } from "@/modules/supply/queries"
import type { DeviceClass } from "@/modules/supply/types"
import {
  IconArrowUpRight,
  IconBuildingBank,
  IconDeviceDesktopAnalytics,
  IconEye,
  IconSettings,
  IconShieldLock,
  IconSortDescending,
} from "@tabler/icons-react"
import { Link, useSearch } from "@tanstack/react-router"
import type { SettingsSection } from "./route"

/**
 * Two kinds of thing live here. **Definitions** are reference data — hardware
 * types, providers, scheduling classes and guardrails the rest of the console
 * points at; they are the plane's, read constantly and written rarely. Beneath
 * them, **Console** settings belong to this browser and are never written over
 * the API, so two operators can hold different views of the same fleet.
 *
 * One section is on screen at a time and the nav picks it, held in `?section=`
 * so a view is linkable and the back button walks it.
 */
export function SettingsPage() {
  const { section } = useSearch({ from: "/_app/settings" })
  const Section = SECTIONS[section]

  return (
    <div className="flex flex-col">
      <PageHeader title="Settings" description="definitions · this console" />

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
  providers: ProvidersSection,
  priority: PriorityClassesSection,
  policies: PoliciesSection,
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
    section: "providers" as const,
    label: "Providers & regions",
    icon: IconBuildingBank,
  },
  {
    section: "priority" as const,
    label: "Priority classes",
    icon: IconSortDescending,
  },
  { section: "policies" as const, label: "Policies", icon: IconShieldLock },
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

// TODO-API providers & regions: the plane has no provider/registration
// registry; these rows are mocked reference data.
const PROVIDERS = [
  {
    id: "aws",
    name: "Amazon Web Services",
    distribution: "eks",
    regions: "us-east-1, eu-central-1, ap-southeast-1",
    clusters: "3 clusters",
  },
  {
    id: "gcp",
    name: "Google Cloud",
    distribution: "gke",
    regions: "us-west-2",
    clusters: "1 cluster",
  },
  {
    id: "azure",
    name: "Microsoft Azure",
    distribution: "aks",
    regions: "us-west-2",
    clusters: "1 cluster",
  },
  {
    id: "equinix",
    name: "Equinix Metal",
    distribution: "bare-metal",
    regions: "us-central, eu-west-2",
    clusters: "2 clusters",
  },
  {
    id: "on-prem",
    name: "On-premises",
    distribution: "kubeadm",
    regions: "us-central",
    clusters: "1 cluster",
  },
]

function ProvidersSection() {
  return (
    <Panel
      id="providers"
      title="Providers & regions"
      description="where clusters can be registered"
    >
      <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Provider</TableHead>
              <TableHead>Name</TableHead>
              <TableHead>Distribution</TableHead>
              <TableHead>Regions</TableHead>
              <TableHead>Clusters</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {PROVIDERS.map((p) => (
              <TableRow key={p.id}>
                <TableCell className="font-mono text-2xs">{p.id}</TableCell>
                <TableCell className="text-xs">{p.name}</TableCell>
                <TableCell className="font-mono text-2xs text-muted-foreground">
                  {p.distribution}
                </TableCell>
                <TableCell className="font-mono text-2xs text-muted-foreground">
                  {p.regions}
                </TableCell>
                <TableCell className="font-mono text-2xs tabular-nums">
                  {p.clusters}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </Panel>
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

/** This browser's own settings — safety, where it points, and appearance. */
/**
 * Three panels rather than one: read-only is a safety switch, appearance is
 * taste, and the plane endpoint is a build-time fact nobody can change from
 * here. Reading them as one list invites the last to be mistaken for a setting.
 */
function PreferencesSection() {
  return (
    <>
      <ReadOnlyPanel />
      <AppearancePanel />
      <PlanePanel />
    </>
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
  const accent = useSettingsStore((s) => s.accent)
  const setAccent = useSettingsStore((s) => s.setAccent)

  return (
    <Panel
      id="appearance"
      title="Appearance"
      description="held in this browser, never written to the plane"
    >
      <div className="space-y-4">
        <div className="space-y-1.5">
          <Label>Accent</Label>
          <p className="text-2xs text-muted-foreground/70">
            Drives the active nav item, resource glyphs, buttons and focus
            rings. Green, amber and red are reserved for ok, warn and danger.
          </p>
          <div className="flex flex-wrap gap-2 pt-1">
            {ACCENTS.map((value) => (
              <button
                key={value}
                type="button"
                onClick={() => setAccent(value as Accent)}
                data-accent={value}
                aria-pressed={accent === value}
                className={
                  accent === value
                    ? "flex items-center gap-2 rounded-md border-2 border-primary bg-primary/10 px-2.5 py-1.5 font-mono text-2xs"
                    : "flex items-center gap-2 rounded-md border px-2.5 py-1.5 font-mono text-2xs hover:bg-accent"
                }
              >
                <span className="size-3.5 rounded-full bg-primary" />
                {value}
              </button>
            ))}
          </div>
        </div>

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
