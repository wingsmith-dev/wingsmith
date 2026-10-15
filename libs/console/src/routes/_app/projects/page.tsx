import { DataTable, type Column } from "@/components/layout/data-table"
import { ResourceCell } from "@/components/layout/resource-cell"
import { DeleteDialog, RowActions } from "@/components/layout/destructive"
import { Dash, Id } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { StatusLabel } from "@/components/layout/status-dot"
import { Button } from "@/components/ui/button"
import { formatError, shortenId } from "@/lib/format"
import { useDeleteProject, useProjects } from "@/modules/binding/queries"
import type { Project } from "@/modules/binding/types"
import { ProjectForm } from "./ProjectForm"
import { useState } from "react"
import { toast } from "sonner"
import { Link } from "@tanstack/react-router"

function columns(
  onEdit: (project: Project) => void,
  onDelete: (project: Project) => void
): Column<Project>[] {
  return [
    {
      key: "slug",
      header: "Project",
      cell: (project) => (
        <ResourceCell kind="project">
          <span className="block">
            <span className="font-mono text-xs">{project.slug}</span>
            <Id value={shortenId(project.id)} className="ml-2" />
          </span>
        </ResourceCell>
      ),
    },
    {
      key: "name",
      header: "Name",
      cell: (project) => <span className="text-xs">{project.name}</span>,
    },
    {
      key: "tenant",
      header: "Tenant",
      cell: (project) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {project.tenant.slug}
          {!project.tenant.enabled && (
            <span className="text-warning"> · disabled</span>
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
        <div className="flex items-center justify-end gap-2">
          <Link
            to="/quotas"
            search={{ project: project.id }}
            className="font-mono text-2xs text-muted-foreground hover:underline"
          >
            quotas →
          </Link>
          <RowActions
            actions={[
              { label: "Edit", onSelect: () => onEdit(project) },
              {
                label: "Delete",
                destructive: true,
                onSelect: () => onDelete(project),
              },
            ]}
          />
        </div>
      ),
    },
  ]
}

/**
 * `enabled` and `effective_enabled` are two columns because a project inside a
 * disabled tenant is still a project somebody enabled, and which of the two
 * switches is off is the thing an operator needs to know.
 */
export function ProjectsPage() {
  const list = useProjects()
  const remove = useDeleteProject()
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Project | undefined>()
  const [doomed, setDoomed] = useState<Project | undefined>()

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Projects"
        description="declared · the tenancy unit quota attaches to"
        actions={<Button onClick={() => setCreating(true)}>New project</Button>}
      />
      <div className="space-y-4 p-4">
        <DataTable
          columns={columns(setEditing, setDoomed)}
          list={list}
          rowKey={(project) => project.id}
          empty="No projects."
        />
      </div>

      {creating && (
        <ProjectForm
          open
          onOpenChange={(open) => !open && setCreating(false)}
        />
      )}
      {editing && (
        <ProjectForm
          key={editing.revision}
          open
          project={editing}
          onOpenChange={(open) => !open && setEditing(undefined)}
        />
      )}
      <DeleteDialog
        icon="project"
        open={!!doomed}
        onOpenChange={(open) => !open && setDoomed(undefined)}
        kind="project"
        name={doomed?.slug ?? ""}
        note="Its quotas go with it."
        error={remove.error}
        pending={remove.isPending}
        onConfirm={() =>
          doomed &&
          remove.mutate(doomed.id, {
            onSuccess: () => {
              setDoomed(undefined)
              toast.success("Project deleted", { description: doomed.slug })
            },
            onError: (e) =>
              toast.error(formatError(e, "Could not delete project")),
          })
        }
      />
    </div>
  )
}
