import { Field, FieldGrid } from "@/components/layout/field"
import { Dash, Labels } from "@/components/layout/mono"
import { PageHeader } from "@/components/layout/page-header"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { formatError } from "@/lib/format"
import { DeleteDialog } from "@/components/layout/destructive"
import { useDeleteEndpoint, useEndpoint } from "@/modules/demand/queries"
import { EndpointForm } from "../EndpointForm"
import { useState } from "react"
import { toast } from "sonner"
import { IconArrowLeft } from "@tabler/icons-react"
import { Link, useNavigate, useParams } from "@tanstack/react-router"

export function EndpointDetailPage() {
  const { endpointId } = useParams({ from: "/_app/endpoints/$endpointId" })
  const { data: endpoint, isLoading, error } = useEndpoint(endpointId)
  const remove = useDeleteEndpoint()
  const navigate = useNavigate()
  const [editing, setEditing] = useState(false)
  const [doomed, setDoomed] = useState(false)

  if (error) {
    return (
      <div className="flex flex-col">
        <PageHeader title="Endpoint" />
        <div className="p-10 text-center text-sm text-destructive">
          {formatError(error, "Failed to load endpoint")}
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col">
      <PageHeader
        title={endpoint?.name ?? "Endpoint"}
        description={endpoint?.id}
        actions={
          <div className="flex gap-2">
            {endpoint && (
              <>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => setEditing(true)}
                >
                  Edit
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => setDoomed(true)}
                >
                  Delete
                </Button>
              </>
            )}
            <Button asChild variant="outline" size="sm">
              <Link to="/endpoints">
                <IconArrowLeft className="size-4" />
                All endpoints
              </Link>
            </Button>
          </div>
        }
      />

      {endpoint && editing && (
        <EndpointForm
          key={endpoint.revision}
          open
          endpoint={endpoint}
          onOpenChange={(open) => !open && setEditing(false)}
        />
      )}
      {endpoint && (
        <DeleteDialog
          icon="endpoint"
          open={doomed}
          onOpenChange={setDoomed}
          kind="endpoint"
          name={endpoint.name}
          note="Clients calling this name start failing immediately."
          error={remove.error}
          pending={remove.isPending}
          onConfirm={() =>
            remove.mutate(endpoint.id, {
              onSuccess: () => {
                setDoomed(false)
                toast.success("Endpoint deleted", {
                  description: endpoint.name,
                })
                navigate({ to: "/endpoints" })
              },
              onError: (e) =>
                toast.error(formatError(e, "Could not delete endpoint")),
            })
          }
        />
      )}

      <div className="space-y-4 p-4">
        {isLoading && <Skeleton className="h-40 w-full" />}

        {endpoint && (
          <>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Declared</CardTitle>
              </CardHeader>
              <CardContent>
                <FieldGrid>
                  <Field label="Name">{endpoint.name}</Field>
                  <Field label="Display name">
                    <Dash>{endpoint.display_name}</Dash>
                  </Field>
                  <Field label="Project">{endpoint.project_id}</Field>
                  <Field label="Visibility">{endpoint.visibility}</Field>
                  <Field label="Routing strategy">
                    {endpoint.routing_strategy}
                  </Field>
                  <Field label="Enabled">
                    {endpoint.enabled ? "yes" : "no"}
                  </Field>
                  <Field label="Labels" className="sm:col-span-2">
                    <Labels labels={endpoint.labels} />
                  </Field>
                </FieldGrid>
              </CardContent>
            </Card>

            <Card className="overflow-hidden p-0">
              <div className="border-b bg-muted/30 px-4 py-3">
                <span className="font-mono text-2xs tracking-[0.18em] text-muted-foreground/50 uppercase">
                  targets
                </span>
                <span className="ml-2 font-mono text-2xs text-muted-foreground/40">
                  which cluster each runs in is a property of the deployment
                </span>
              </div>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Deployment</TableHead>
                    <TableHead>Role</TableHead>
                    <TableHead className="text-right">Weight</TableHead>
                    <TableHead className="text-right">Enabled</TableHead>
                    <TableHead className="text-right">Failover order</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {endpoint.targets.map((target) => {
                    const order = endpoint.failover_order.indexOf(
                      target.deployment_id
                    )
                    return (
                      <TableRow key={target.deployment_id}>
                        <TableCell>
                          <Link
                            to="/deployments/$deploymentId"
                            params={{ deploymentId: target.deployment_id }}
                            className="font-mono text-xs hover:underline"
                          >
                            {target.deployment_id}
                          </Link>
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant="secondary"
                            className="font-mono text-2xs"
                          >
                            {target.role}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-right font-mono text-xs">
                          {target.weight}
                        </TableCell>
                        <TableCell className="text-right font-mono text-2xs">
                          {target.enabled ? "yes" : "no"}
                        </TableCell>
                        <TableCell className="text-right font-mono text-2xs text-muted-foreground">
                          {order === -1 ? "—" : order + 1}
                        </TableCell>
                      </TableRow>
                    )
                  })}
                  {endpoint.targets.length === 0 && (
                    <TableRow>
                      <TableCell
                        colSpan={5}
                        className="py-10 text-center text-sm text-muted-foreground"
                      >
                        No targets — this endpoint routes nowhere.
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </Card>
          </>
        )}
      </div>
    </div>
  )
}
