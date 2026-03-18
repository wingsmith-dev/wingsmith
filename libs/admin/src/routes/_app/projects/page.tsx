import { DataTable, type Column } from "@/components/layout/data-table"
import { Dash, Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { StatusLabel } from "@/components/layout/status-dot"
import { useProjects } from "@/modules/binding/queries"
import type { Project } from "@/modules/binding/types"
import { Link } from "@tanstack/react-router"

const columns: Column<Project>[] = [
  {
    key: "slug",
    header: "Project",
    cell: (project) => (
      <span className="block">
        <span className="font-mono text-xs">{project.slug}</span>
        <Id value={project.id} className="ml-2" />
      </span>
    ),
  },
  { key: "name", header: "Name", cell: (project) => <span className="text-xs">{project.name}</span> },
  {
    key: "tenant",
    header: "Tenant",
    cell: (project) => (
      <span className="font-mono text-2xs text-muted-foreground">
        {project.tenant.slug}
        {!project.tenant.enabled && (
          <span className="text-amber-600 dark:text-amber-400"> · disabled</span>
        )}
      </span>
    ),
  },
  {
    key: "priority",
    header: "Default priority",
    cell: (project) => (
      <span className="font-mono text-2xs text-muted-foreground">
        <Dash>{project.default_priority_class}</Dash>
      </span>
    ),
  },
  {
    key: "enabled",
    header: "Enabled",
    cell: (project) => (
      <StatusLabel tone={project.enabled ? "ok" : "neutral"}>
        {project.enabled ? "yes" : "no"}
      </StatusLabel>
    ),
  },
  {
    key: "effective",
    header: "Effective",
    cell: (project) => (
      <StatusLabel tone={project.effective_enabled ? "ok" : "danger"}>
        {project.effective_enabled ? "schedulable" : "blocked"}
      </StatusLabel>
    ),
  },
  {
    key: "quotas",
    header: "",
    align: "right",
    cell: (project) => (
      <Link
        to="/quotas"
        search={{ project: project.id }}
        className="font-mono text-2xs text-muted-foreground hover:underline"
      >
        quotas →
      </Link>
    ),
  },
]

/**
 * `enabled` and `effective_enabled` are two columns because a project inside a
 * disabled tenant is still a project somebody enabled, and which of the two
 * switches is off is the thing an operator needs to know.
 */
export function ProjectsPage() {
  const list = useProjects()
  return (
    <div className="flex flex-col">
      <PageHeader title="Projects" description="declared · the tenancy unit quota attaches to" />
      <div className="space-y-4 p-4">
        <DataTable
          columns={columns}
          list={list}
          rowKey={(project) => project.id}
          empty="No projects."
        />
      </div>
    </div>
  )
}
