import { Link } from "@tanstack/react-router"
import { DeploymentTimeline } from "@/components/demand/deployment-timeline"
import { DataTable } from "@/components/layout/data-table"
import { Field, FieldGrid } from "@/components/layout/field"
import { Panel } from "@/components/layout/panel"
import { StatRow, StatTile } from "@/components/layout/stat-tile"
import type { Workload, WorkloadPlacement } from "@/modules/demand/types"
import { replicaColumns } from "./Columns"
import { SeeAllEvents, staticList } from "./Common"
import { EditButton } from "./EditButton"
import { GpuNodeCard, LegendSwatch } from "./Performance"

export function PlacementView({
  workload,
  placement,
  recentEvents,
  eventsLoading,
  onEditPlacement,
  onSeeAllEvents,
}: {
  workload: Workload
  placement: WorkloadPlacement | undefined
  recentEvents: React.ComponentProps<typeof DeploymentTimeline>["events"]
  eventsLoading: boolean
  onEditPlacement: () => void
  onSeeAllEvents: () => void
}) {
  return (
    <>
      <StatRow>
        <StatTile
          label="Nodes"
          value={placement ? String(placement.node_count) : "—"}
        />
        <StatTile
          label="GPUs allocated"
          value={placement ? String(placement.gpu_count) : "—"}
        />
      </StatRow>

      <div className="grid gap-4 md:grid-cols-[2fr_1fr]">
        <div className="flex min-w-0 flex-col gap-4">
          <Panel
            title="Topology"
            description={`GPU allocation across ${
              placement?.node_count ?? 0
            } nodes in ${workload.deployments.length} clusters`}
          >
            <div className="flex flex-col gap-3">
              {(placement?.nodes ?? []).map((node) => (
                <GpuNodeCard key={node.node} node={node} />
              ))}
              {placement && placement.nodes.length === 0 && (
                <p className="text-2xs text-muted-foreground">
                  This workload holds no devices right now.
                </p>
              )}
            </div>
            <div className="mt-3 flex flex-wrap gap-4 text-2xs text-muted-foreground">
              <LegendSwatch>This workload</LegendSwatch>
            </div>
          </Panel>

          <Panel
            title="Replica placement"
            description="Node and GPU assignment for each live allocation"
          >
            <DataTable
              columns={replicaColumns}
              list={staticList(placement?.replicas ?? [])}
              rowKey={(r) => r.allocation}
              empty="No replicas placed."
            />
          </Panel>
        </div>

        <div className="flex min-w-0 flex-col gap-4">
          <Panel
            title="Scheduling constraints"
            action={
              <EditButton
                label="Edit placement policy"
                onClick={() => onEditPlacement()}
              />
            }
          >
            <FieldGrid>
              <Field label="Priority class">{workload.priority_class}</Field>
              <Field label="Preemptible">
                {workload.preemptible ? "yes" : "no"}
              </Field>
              <Field label="Strategy">{workload.strategy}</Field>
              <Field label="Fan-out cap">
                {workload.max_clusters ?? "none"}
              </Field>
              <Field label="Pinned cluster">
                {workload.pinned_cluster_id ? (
                  <Link
                    to="/clusters/$clusterId"
                    params={{
                      clusterId: workload.pinned_cluster_id,
                    }}
                    className="hover:underline"
                  >
                    {workload.pinned_cluster_id}
                  </Link>
                ) : (
                  "none"
                )}
              </Field>
            </FieldGrid>
          </Panel>

          <Panel
            title="Recent scheduling events"
            action={<SeeAllEvents onClick={() => onSeeAllEvents()} />}
          >
            <DeploymentTimeline events={recentEvents} loading={eventsLoading} />
          </Panel>
        </div>
      </div>
    </>
  )
}
