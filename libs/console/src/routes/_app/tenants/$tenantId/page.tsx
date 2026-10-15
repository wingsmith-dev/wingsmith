import { useState } from "react"
import {
  IconArrowLeft,
  IconBolt,
  IconKey,
  IconLayoutDashboard,
} from "@tabler/icons-react"
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router"

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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { formatAbsolute, formatError, formatRelative } from "@/lib/format"
import { useProjects, useTenant } from "@/modules/binding/queries"
import type { Project, Tenant } from "@/modules/binding/types"

import type { TenantTab } from "./route"
import { TenantForm } from "../TenantForm"
import { TenantKeys } from "./Keys"
import { TenantServed } from "./Served"

/**
 * The three charts a tenant's traffic is read from, and three because that is
 * what fills the lead row — `MetricsPanel` gives one lead metric the whole width,
 * which turns a sparkline into a stripe across an empty card.
 *
 * How much capacity they are holding, how long they wait for a first token, and how
 * fast it comes out once it does. The rest are tiles below.
 */
const TRAFFIC_LEAD = [
  "serving.requests_in_flight",
  "serving.ttft_p50",
  "serving.output_tokens_per_second",
]

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

function ViewTab({
  value,
  icon: Icon,
  children,
}: React.PropsWithChildren<{
  value: string
  icon: typeof IconLayoutDashboard
}>) {
  return (
    <TabsTrigger value={value}>
      <Icon />
      {children}
    </TabsTrigger>
  )
}

function Identity({ tenant }: { tenant: Tenant }) {
  return (
    <Card className="p-4">
      <FieldGrid>
        <Field label="Tenant">
          {tenant.slug}
          <CopyableId
            value={tenant.id}
            className="ml-2 text-2xs text-muted-foreground"
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
  const { tab } = useSearch({ from: "/_app/tenants/$tenantId" })
  const navigate = useNavigate({ from: "/tenants/$tenantId" })
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

      <Tabs
        value={tab ?? "overview"}
        onValueChange={(next) =>
          navigate({ search: { tab: next as TenantTab }, replace: true })
        }
        className="flex-col gap-0"
      >
        <TabsList variant="underline" className="bg-card px-4">
          <ViewTab value="overview" icon={IconLayoutDashboard}>
            Overview
          </ViewTab>
          <ViewTab value="serving" icon={IconBolt}>
            Serving
          </ViewTab>
          <ViewTab value="keys" icon={IconKey}>
            Keys
          </ViewTab>
        </TabsList>

        <div className="space-y-4 p-4">
          {isLoading && <Skeleton className="h-40 w-full" />}

          <TabsContent value="overview" className="space-y-4">
            {tenant && <Identity tenant={tenant} />}

            {/*
             * The lead charts only. Enough to say whether this tenant is worth
             * opening Serving for, and not so much that the answer is buried.
             */}
            {tenant && (
              <div>
                <h2 className="mb-2 font-mono text-2xs tracking-[0.18em] text-muted-foreground uppercase">
                  Traffic
                </h2>
                <MetricsPanel
                  scope="tenant"
                  refId={tenant.id}
                  family="serving"
                  lead={TRAFFIC_LEAD}
                  leadOnly
                />
              </div>
            )}

            <div>
              <h2 className="mb-2 font-mono text-2xs tracking-[0.18em] text-muted-foreground uppercase">
                Projects
              </h2>
              <DataTable
                columns={PROJECT_COLUMNS}
                list={projects}
                rowKey={(project) => project.id}
                empty="No projects. Nothing can be deployed or served for this tenant until one exists."
              />
            </div>
          </TabsContent>

          <TabsContent value="serving" className="space-y-4">
            {tenant && (
              <MetricsPanel
                scope="tenant"
                refId={tenant.id}
                family="serving"
                lead={TRAFFIC_LEAD}
              />
            )}
            {tenant && <TenantServed tenant={tenant.id} />}
          </TabsContent>

          <TabsContent value="keys">
            {tenant && <TenantKeys tenant={tenant.id} />}
          </TabsContent>
        </div>
      </Tabs>

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
