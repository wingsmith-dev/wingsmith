import { useState } from "react"
import { IconArrowLeft } from "@tabler/icons-react"
import { Link, useParams } from "@tanstack/react-router"

import { DataTable, type Column } from "@/components/layout/data-table"
import { Field, FieldGrid } from "@/components/layout/field"
import { CopyableId, Dash } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { RefreshButton } from "@/components/layout/refresh-button"
import { ResourceCell } from "@/components/layout/resource-cell"
import { StatusLabel } from "@/components/layout/status-dot"
import { MetricsPanel } from "@/components/metrics/metrics-panel"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import { useProjects, useTenant } from "@/modules/binding/queries"
import type { Project, Tenant } from "@/modules/binding/types"

import { TenantForm } from "../TenantForm"
import { TenantKeys } from "./Keys"
import { TenantServed } from "./Served"

const PROJECT_COLUMNS: Column<Project>[] = [
  {
    key: "slug",
    header: "Project",
    cell: (project) => (
      <ResourceCell kind="project">
        <span className="font-mono text-xs">{project.slug}</span>
      </ResourceCell>
    ),
  },
  {
    key: "name",
    header: "Name",
    cell: (project) => <span className="text-xs">{project.name}</span>,
  },
  {
    key: "priority",
    header: "Default priority",
    secondary: true,
    cell: (project) => (
      <span className="font-mono text-2xs text-muted-foreground">
        <Dash>{project.default_priority_class}</Dash>
      </span>
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
    key: "links",
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

function Identity({ tenant }: { tenant: Tenant }) {
  return (
    <Card className="p-4">
      <FieldGrid>
        <Field label="Tenant">
          {tenant.slug}
          <CopyableId
            value={tenant.id}
            className="ml-2 text-2xs text-muted-foreground/60"
          />
        </Field>
        <Field label="Name">{tenant.name}</Field>
        <Field label="Enabled">
          <StatusLabel tone={tenant.enabled ? "ok" : "danger"}>
            {tenant.enabled ? "yes" : "no"}
          </StatusLabel>
        </Field>
        <Field label="Ingress">
          <span className="font-mono text-xs">{tenant.ingress}</span>
        </Field>
        <Field label="Auth">
          <span className="font-mono text-xs">{tenant.auth}</span>
        </Field>
        <Field label="Host">
          <span className="font-mono text-xs">
            <Dash>{tenant.host}</Dash>
          </span>
        </Field>
        <Field label="Projects">{tenant.project_count}</Field>
        <Field label="Created">{formatAbsolute(tenant.created_at)}</Field>
        <Field label="Updated">
          <span title={formatAbsolute(tenant.updated_at)}>
            {formatRelative(tenant.updated_at)}
          </span>
        </Field>
      </FieldGrid>
    </Card>
  )
}

/**
 * One tenant, from the two directions it is looked at: what we declared about
 * them, and what they are actually being served. The second half is the tenant's
 * own page — a support conversation starts with what the customer can see, and
 * an operator who has to guess at that is answering a different question.
 *
 * The route parameter is a slug or an id — either resolves — and the serving
 * read is given the tenant's uid off the loaded record rather than the parameter,
 * so the cache is keyed by identity and two links to the same tenant do not read
 * it twice.
 */
export function TenantDetailPage() {
  const { tenantId } = useParams({ from: "/_app/tenants/$tenantId" })
  const { data: tenant, isLoading, error } = useTenant(tenantId)
  const projects = useProjects({ tenant: tenantId })
  const [editing, setEditing] = useState(false)

  if (error) {
    return (
      <div className="flex flex-col">
        <PageHeader title="Tenant" />
        <div className="p-10 text-center text-sm text-destructive">
          {formatError(error, "Failed to load tenant")}
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col">
      <PageHeader
        back={
          <Link
            to="/tenants"
            aria-label="All tenants"
            title="All tenants"
            className="flex size-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <IconArrowLeft className="size-4" />
          </Link>
        }
        title={tenant?.name ?? "Tenant"}
        description={tenant ? `${tenant.slug} · ${tenant.id}` : undefined}
        readActions={
          <RefreshButton keys={[["tenants"], ["projects"], ["serving"]]} />
        }
        actions={
          tenant ? (
            <Button variant="outline" onClick={() => setEditing(true)}>
              Edit
            </Button>
          ) : undefined
        }
      />

      <div className="space-y-4 p-4">
        {isLoading && <Skeleton className="h-40 w-full" />}
        {tenant && <Identity tenant={tenant} />}

        {tenant && (
          <div>
            <h2 className="mb-2 font-mono text-2xs tracking-[0.18em] text-muted-foreground/50 uppercase">
              Traffic
            </h2>
            <MetricsPanel
              scope="tenant"
              refId={tenant.id}
              family="serving"
              lead={["serving.requests_in_flight"]}
            />
          </div>
        )}

        {tenant && <TenantServed tenant={tenant.id} />}

        {tenant && <TenantKeys tenant={tenant.id} />}

        <div>
          <h2 className="mb-2 font-mono text-2xs tracking-[0.18em] text-muted-foreground/50 uppercase">
            Projects
          </h2>
          <DataTable
            columns={PROJECT_COLUMNS}
            list={projects}
            rowKey={(project) => project.id}
            empty="No projects. Nothing can be deployed or served for this tenant until one exists."
          />
        </div>
      </div>

      {tenant && editing && (
        <TenantForm
          key={tenant.revision}
          open
          tenant={tenant}
          onOpenChange={setEditing}
        />
      )}
    </div>
  )
}
