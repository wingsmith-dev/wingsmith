import { StatusLabel } from "@/components/layout/status-dot"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { formatRelative } from "@/lib/format"
import { COMPONENT_PHASE_TONE, humanize } from "@/modules/clusters/display"
import type { ClusterComponent } from "@/modules/clusters/types"

export function ComponentsTable({
  components,
}: {
  components: ClusterComponent[]
}) {
  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Component</TableHead>
            <TableHead>Kind</TableHead>
            <TableHead>Declared</TableHead>
            <TableHead>Observed</TableHead>
            <TableHead>Phase</TableHead>
            <TableHead className="text-right">Updated</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {components.map((component) => (
            <TableRow key={component.name}>
              <TableCell className="font-mono text-xs">
                {component.name}
              </TableCell>
              <TableCell className="font-mono text-2xs text-muted-foreground">
                {component.kind}
              </TableCell>
              <TableCell className="font-mono text-2xs">
                {component.declared_version}
              </TableCell>
              <TableCell className="font-mono text-2xs">
                {component.observed_version ?? "—"}
              </TableCell>
              <TableCell>
                <StatusLabel tone={COMPONENT_PHASE_TONE[component.phase]}>
                  {humanize(component.phase)}
                </StatusLabel>
              </TableCell>
              <TableCell className="text-right font-mono text-2xs text-muted-foreground/70">
                {formatRelative(component.last_updated_at)}
              </TableCell>
            </TableRow>
          ))}

          {components.length === 0 && (
            <TableRow>
              <TableCell
                colSpan={6}
                className="py-10 text-center text-sm text-muted-foreground"
              >
                No components installed.
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </div>
  )
}
