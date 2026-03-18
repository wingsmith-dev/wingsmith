import { Field, FieldGrid } from "@/components/layout/field"
import { Labels } from "@/components/layout/mono"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Dash } from "@/components/layout/mono"
import { formatAbsolute } from "@/lib/format"
import type { Cluster } from "@/modules/clusters/types"

/**
 * The declared half. Capacity is not here on purpose: the plane does not
 * denormalise node and device counts onto a cluster, and inventing the rollup
 * client-side would put a number on screen that nothing on the server agrees
 * with. Counts come from the tabs below, which read the collections directly.
 */
export function DeclaredCard({ cluster }: { cluster: Cluster }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Declared</CardTitle>
      </CardHeader>
      <CardContent>
        <FieldGrid>
          <Field label="Name">{cluster.name}</Field>
          <Field label="Display name">
            <Dash>{cluster.display_name}</Dash>
          </Field>
          <Field label="Region">
            {cluster.region}
            {cluster.zone && ` · ${cluster.zone}`}
          </Field>
          <Field label="Residency">
            <Dash>{cluster.residency}</Dash>
          </Field>
          <Field label="Enrolled">
            {cluster.enrolled_at ? formatAbsolute(cluster.enrolled_at) : "not enrolled"}
          </Field>
          <Field label="Revision">{cluster.revision}</Field>
          <Field label="Labels" className="sm:col-span-2">
            <Labels labels={cluster.labels} />
          </Field>
          <Field label="Capabilities" className="sm:col-span-2">
            {Object.keys(cluster.capabilities).length === 0 ? (
              <span className="text-muted-foreground/40">none</span>
            ) : (
              Object.entries(cluster.capabilities)
                .map(([key, value]) => (value === true ? key : `${key}=${String(value)}`))
                .join(" · ")
            )}
          </Field>
        </FieldGrid>
      </CardContent>
    </Card>
  )
}
