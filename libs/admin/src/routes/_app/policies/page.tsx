import { DataTable, type Column } from "@/components/layout/data-table"
import { Dash, Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { StatusLabel } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { usePolicies } from "@/modules/binding/queries"
import type { Policy } from "@/modules/binding/types"

function List({ values, tone }: { values: string[]; tone: "allow" | "deny" }) {
  if (values.length === 0) {
    return (
      <span className="font-mono text-2xs text-muted-foreground/40">
        {tone === "allow" ? "no opinion" : "none"}
      </span>
    )
  }
  return (
    <div className="flex flex-wrap gap-1">
      {values.map((value) => (
        <Badge
          key={value}
          variant="outline"
          className={
            tone === "deny"
              ? "border-destructive/40 font-mono text-2xs text-destructive"
              : "font-mono text-2xs"
          }
        >
          {value}
        </Badge>
      ))}
    </div>
  )
}

const columns: Column<Policy>[] = [
  {
    key: "name",
    header: "Policy",
    cell: (policy) => (
      <span className="block">
        <span className="font-mono text-xs">{policy.name}</span>
        <Id value={policy.id} className="ml-2" />
      </span>
    ),
  },
  {
    key: "description",
    header: "Description",
    cell: (policy) => (
      <span className="text-xs text-muted-foreground">
        <Dash>{policy.description}</Dash>
      </span>
    ),
  },
  {
    key: "regions",
    header: "Allowed regions",
    cell: (policy) => <List values={policy.allowed_regions} tone="allow" />,
  },
  {
    key: "residencies",
    header: "Residencies",
    secondary: true,
    cell: (policy) => <List values={policy.allowed_residencies} tone="allow" />,
  },
  {
    key: "denied",
    header: "Denied clusters",
    cell: (policy) => <List values={policy.denied_clusters} tone="deny" />,
  },
  {
    key: "cost",
    header: "Max hourly cost",
    align: "right",
    cell: (policy) => (
      <span className="font-mono text-2xs">
        {policy.max_hourly_cost === null ? "—" : policy.max_hourly_cost}
      </span>
    ),
  },
  {
    key: "attachments",
    header: "Attached to",
    cell: (policy) =>
      policy.attachments.length === 0 ? (
        <span className="font-mono text-2xs text-muted-foreground/40">
          nothing — constrains nothing
        </span>
      ) : (
        <span className="font-mono text-2xs text-muted-foreground">
          {policy.attachments
            .map((a) => `${a.subject_type}:${a.subject_name ?? a.subject ?? a.subject_id}`)
            .join(" ")}
        </span>
      ),
  },
  {
    key: "enabled",
    header: "Enabled",
    align: "right",
    cell: (policy) => (
      <StatusLabel tone={policy.enabled ? "ok" : "neutral"}>
        {policy.enabled ? "yes" : "no"}
      </StatusLabel>
    ),
  },
]

/**
 * Allow-lists and deny-lists are separate columns: an empty allow-list means
 * "no opinion", not "nothing allowed", and a denial has to survive somebody
 * widening the allow-list later.
 */
export function PoliciesPage() {
  const list = usePolicies()
  return (
    <div className="flex flex-col">
      <PageHeader
        title="Policies"
        description="declared · placement constraints and what they bind to"
      />
      <div className="space-y-4 p-4">
        <DataTable
          columns={columns}
          list={list}
          rowKey={(policy) => policy.id}
          empty="No policies declared."
        />
      </div>
    </div>
  )
}
