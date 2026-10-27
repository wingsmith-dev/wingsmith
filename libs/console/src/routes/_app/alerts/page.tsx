import { AlertDetail } from "@/components/alerts/alert-detail"
import { ALL, FilterSelect } from "@/components/layout/filter-select"
import { DataTable, type Column } from "@/components/layout/data-table"
import { DeleteDialog, RowActions } from "@/components/layout/destructive"
import { Dash } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { RefreshButton } from "@/components/layout/refresh-button"
import { StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { formatError, formatRelative, formatAbsolute } from "@/lib/format"
import {
  ALERT_SEVERITIES,
  ALERT_STATES,
  alertStateTone,
  comparisonWord,
  effectiveForSeconds,
  effectiveLookback,
  effectiveSeverity,
  formatSeconds,
  formatThresholdValue,
  precedenceNote,
} from "@/modules/alerts/display"
import {
  useAlertRules,
  useAlertThresholdOptions,
  useAlertThresholds,
  useAlerts,
  useDeleteAlertThreshold,
} from "@/modules/alerts/queries"
import type { Alert, AlertThreshold, MetricRule } from "@/modules/alerts/types"
import { clusterLabel } from "@/modules/clusters/display"
import { useClusterOptions } from "@/modules/clusters/queries"
import { severityTone } from "@/modules/machinery/display"
import { useNavigate, useSearch } from "@tanstack/react-router"
import { ThresholdForm } from "./ThresholdForm"
import { useState } from "react"
import { toast } from "sonner"

const alertColumns: Column<Alert>[] = [
  {
    key: "state",
    header: "State",
    cell: (alert) => (
      <StatusLabel
        tone={alertStateTone(alert.state)}
        pulse={alert.state === "firing"}
      >
        {alert.state}
      </StatusLabel>
    ),
  },
  {
    key: "severity",
    header: "Severity",
    cell: (alert) => (
      <StatusLabel tone={severityTone(alert.severity)}>
        {alert.severity}
      </StatusLabel>
    ),
  },
  {
    key: "rule",
    header: "Rule",
    cell: (alert) => <span className="font-mono text-xs">{alert.rule}</span>,
  },
  {
    key: "title",
    header: "What happened",
    cell: (alert) => <span className="text-xs">{alert.title}</span>,
  },
  {
    key: "subject",
    header: "Subject",
    secondary: true,
    cell: (alert) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {alert.subject.type}
        {alert.subject.id && (
          <span className="text-muted-foreground"> · {alert.subject.id}</span>
        )}
      </span>
    ),
  },
  {
    key: "since",
    header: "Since",
    cell: (alert) => (
      <span
        className="font-mono text-2xs text-muted-foreground"
        title={formatAbsolute(alert.fired_at ?? alert.first_seen_at)}
      >
        {formatRelative(alert.fired_at ?? alert.first_seen_at)}
      </span>
    ),
  },
  {
    key: "occurrences",
    header: "Seen",
    align: "right",
    secondary: true,
    cell: (alert) => (
      <span className="font-mono text-xs">{alert.occurrences}</span>
    ),
  },
]

function thresholdColumns(
  rules: MetricRule[],
  all: AlertThreshold[],
  onEdit: (threshold: AlertThreshold) => void,
  onDelete: (threshold: AlertThreshold) => void
): Column<AlertThreshold>[] {
  const definitionOf = (threshold: AlertThreshold) =>
    rules.find((entry) => entry.name === threshold.rule)

  return [
    {
      key: "rule",
      header: "Rule",
      cell: (threshold) => (
        <div className="min-w-0">
          <div className="font-mono text-xs">{threshold.rule}</div>
          <div className="font-mono text-2xs text-muted-foreground">
            {threshold.metric}
          </div>
        </div>
      ),
    },
    {
      key: "scope",
      header: "Scope",
      cell: (threshold) =>
        threshold.subject.id === null ? (
          <Badge variant="outline" className="font-mono text-2xs">
            fleet default
          </Badge>
        ) : (
          <span className="font-mono text-2xs">{threshold.subject.id}</span>
        ),
    },
    {
      key: "precedence",
      header: "Precedence",
      cell: (threshold) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {precedenceNote(threshold, all)}
        </span>
      ),
    },
    {
      key: "value",
      header: "Fires",
      cell: (threshold) => (
        <span className="font-mono text-xs">
          {comparisonWord(threshold.comparison)}{" "}
          {formatThresholdValue(threshold.value, definitionOf(threshold)?.unit)}
        </span>
      ),
    },
    {
      key: "window",
      header: "Window",
      secondary: true,
      cell: (threshold) => {
        const lookback = effectiveLookback(threshold, definitionOf(threshold))
        const hold = effectiveForSeconds(threshold, definitionOf(threshold))
        return (
          <span className="font-mono text-2xs text-muted-foreground">
            {lookback.value ?? "—"}
            {lookback.fromRule && (
              <span className="text-muted-foreground">*</span>
            )}
            {" · for "}
            {hold.value === null ? "—" : formatSeconds(hold.value)}
            {hold.fromRule && <span className="text-muted-foreground">*</span>}
          </span>
        )
      },
    },
    {
      key: "severity",
      header: "Severity",
      secondary: true,
      cell: (threshold) => {
        const severity = effectiveSeverity(threshold, definitionOf(threshold))
        return severity.value ? (
          <span className="font-mono text-2xs text-muted-foreground">
            {severity.value}
            {severity.fromRule && (
              <span className="text-muted-foreground">*</span>
            )}
          </span>
        ) : (
          <Dash>{null}</Dash>
        )
      },
    },
    {
      key: "reason",
      header: "Because",
      secondary: true,
      cell: (threshold) => (
        <span className="text-2xs text-muted-foreground">
          <Dash>{threshold.reason}</Dash>
        </span>
      ),
    },
    {
      key: "enabled",
      header: "Enabled",
      cell: (threshold) =>
        threshold.enabled ? (
          <StatusLabel tone="ok">on</StatusLabel>
        ) : (
          <StatusLabel tone="neutral">off</StatusLabel>
        ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (threshold) => (
        <RowActions
          actions={[
            { label: "Edit", onSelect: () => onEdit(threshold) },
            {
              label: "Delete",
              destructive: true,
              onSelect: () => onDelete(threshold),
            },
          ]}
        />
      ),
    },
  ]
}

/**
 * Alerting, both halves, kept apart.
 *
 * The thresholds are declared and fully writable; the alerts are derived and
 * carry no edit affordance at all — only the two lifecycle verbs the plane
 * offers, and those live in the detail sheet beside the number that produced
 * the alert. Which number that is, and whether it is the fleet default or an
 * override, is the question this screen exists to answer: an operator who
 * cannot see that the fleet default is what is firing will change the wrong row.
 */
export function AlertsPage() {
  const search = useSearch({ from: "/_app/alerts" })
  const navigate = useNavigate({ from: "/alerts" })
  const { data: clusters } = useClusterOptions()
  const { data: rules } = useAlertRules()
  const { data: allThresholds } = useAlertThresholdOptions()

  const alerts = useAlerts({
    state: search.state,
    severity: search.severity,
    rule: search.rule,
    cluster: search.cluster,
    subject: search.subject,
  })
  const thresholds = useAlertThresholds({
    rule: search.rule,
    enabled: search.enabled === undefined ? undefined : search.enabled === "on",
    fleet_default:
      search.scope === undefined ? undefined : search.scope === "fleet",
  })

  const remove = useDeleteAlertThreshold()
  const [opened, setOpened] = useState<Alert | undefined>()
  const [editing, setEditing] = useState<AlertThreshold | undefined>()
  const [doomed, setDoomed] = useState<AlertThreshold | undefined>()
  const [creating, setCreating] = useState<
    { rule?: string; subjectId?: string | null } | undefined
  >()

  const tab = search.tab ?? "firing"
  const catalog = rules ?? []
  const declared = allThresholds ?? []
  const undeclared = catalog.filter(
    (entry) => !declared.some((threshold) => threshold.rule === entry.name)
  )

  const set =
    (key: "state" | "severity" | "rule" | "cluster" | "scope" | "enabled") =>
    (value: string) =>
      navigate({
        search: (prev) => ({
          ...prev,
          [key]: value === ALL ? undefined : value,
        }),
      })

  const firedRules = [
    ...new Set((alerts.items ?? []).map((a) => a.rule)),
  ].sort()

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Alerts"
        description="derived alerts · declared thresholds, and never the same table"
        readActions={
          <RefreshButton keys={[["alerts"], ["alert-thresholds"]]} />
        }
        actions={<Button onClick={() => setCreating({})}>New threshold</Button>}
      />

      <div className="space-y-4 p-4">
        <Tabs
          value={tab}
          onValueChange={(next) =>
            navigate({ search: (prev) => ({ ...prev, tab: next }) })
          }
        >
          <TabsList>
            <TabsTrigger value="firing">Alerts</TabsTrigger>
            <TabsTrigger value="thresholds">Thresholds</TabsTrigger>
          </TabsList>

          <TabsContent value="firing" className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <FilterSelect
                label="State"
                value={search.state ?? ALL}
                onChange={set("state")}
                options={ALERT_STATES.map((s) => ({ value: s, label: s }))}
              />
              <FilterSelect
                label="Severity"
                value={search.severity ?? ALL}
                onChange={set("severity")}
                options={ALERT_SEVERITIES.map((s) => ({ value: s, label: s }))}
              />
              <FilterSelect
                label="Rule"
                value={search.rule ?? ALL}
                onChange={set("rule")}
                options={firedRules.map((r) => ({ value: r, label: r }))}
              />
              <FilterSelect
                label="Cluster"
                value={search.cluster ?? ALL}
                onChange={set("cluster")}
                options={(clusters ?? []).map((cluster) => ({
                  value: cluster.id,
                  label: clusterLabel(cluster),
                }))}
              />
            </div>

            <DataTable
              columns={alertColumns}
              list={alerts}
              rowKey={(alert) => alert.id}
              onRowClick={setOpened}
              empty="No alerts. Nothing is wrong, or nothing declared a number to notice it."
            />
          </TabsContent>

          <TabsContent value="thresholds" className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <FilterSelect
                label="Rule"
                value={search.rule ?? ALL}
                onChange={set("rule")}
                options={catalog.map((entry) => ({
                  value: entry.name,
                  label: entry.name,
                }))}
              />
              <FilterSelect
                label="Scope"
                value={search.scope ?? ALL}
                onChange={set("scope")}
                options={[
                  { value: "fleet", label: "fleet defaults" },
                  { value: "subject", label: "overrides" },
                ]}
              />
              <FilterSelect
                label="Enabled"
                value={search.enabled ?? ALL}
                onChange={set("enabled")}
                options={[
                  { value: "on", label: "enabled" },
                  { value: "off", label: "disabled" },
                ]}
              />
            </div>

            {undeclared.length > 0 && (
              <p className="rounded-md border bg-muted/40 px-3 py-2 font-mono text-2xs text-muted-foreground">
                {undeclared.length} of {catalog.length} metric rules have no
                declared number, so the reactor evaluates them for nobody:{" "}
                <span className="text-muted-foreground">
                  {undeclared.map((entry) => entry.name).join(", ")}
                </span>
              </p>
            )}

            <DataTable
              columns={thresholdColumns(
                catalog,
                declared,
                setEditing,
                setDoomed
              )}
              list={thresholds}
              rowKey={(threshold) => threshold.id}
              empty="No thresholds declared."
            />

            <p className="px-1 font-mono text-2xs text-muted-foreground">
              * inherited from the rule rather than declared on the row.
            </p>
          </TabsContent>
        </Tabs>
      </div>

      {opened && (
        <AlertDetail
          alert={opened}
          thresholds={declared}
          rules={catalog}
          open
          onOpenChange={(open) => !open && setOpened(undefined)}
          onDeclare={(rule, subjectUid) =>
            setCreating({ rule, subjectId: subjectUid })
          }
          onEdit={setEditing}
        />
      )}

      {creating && (
        <ThresholdForm
          open
          defaultRule={creating.rule}
          defaultSubjectId={creating.subjectId}
          onOpenChange={(open) => !open && setCreating(undefined)}
        />
      )}
      {editing && (
        <ThresholdForm
          key={editing.id + (editing.updated_at ?? "")}
          open
          threshold={editing}
          onOpenChange={(open) => !open && setEditing(undefined)}
        />
      )}

      <DeleteDialog
        open={!!doomed}
        onOpenChange={(open) => !open && setDoomed(undefined)}
        kind="threshold"
        name={`${doomed?.rule ?? ""} · ${doomed?.subject.id ?? "fleet default"}`}
        note={
          doomed?.subject.id
            ? "The fleet default applies to that subject again."
            : "Nothing watches that rule for subjects with no override."
        }
        error={remove.error}
        pending={remove.isPending}
        onConfirm={() =>
          doomed &&
          remove.mutate(doomed.id, {
            onSuccess: () => {
              setDoomed(undefined)
              toast.success("Threshold deleted", { description: doomed.rule })
            },
            onError: (e) =>
              toast.error(formatError(e, "Could not delete threshold")),
          })
        }
      />
    </div>
  )
}
