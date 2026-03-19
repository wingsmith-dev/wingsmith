import type { Cluster } from "@/modules/clusters/types"
import {
  useClusterDeployments,
  useClusterDevices,
  useClusterEvents,
  useClusterNodes,
  useClusterOpenOperations,
  useClusterPools,
} from "@/modules/overview/queries"
import {
  deriveClusterActionables,
  summariseDevices,
  summariseNodes,
} from "@/modules/overview/summary"
import { Actionables, type SourceFailure } from "./Actionables"
import { ClusterEvents } from "./ClusterEvents"
import { ClusterIdentity, GlanceTiles } from "./Glance"

/**
 * Is this cluster healthy, and is there anything to do about it — in that
 * order. The actionable list is derived from the same drained reads the counts
 * below it are, so a row and a tile can never disagree.
 */
export function ClusterOverviewTab({ cluster }: { cluster: Cluster }) {
  const pools = useClusterPools(cluster.id)
  const nodes = useClusterNodes(cluster.id)
  const devices = useClusterDevices(cluster.id)
  const deployments = useClusterDeployments(cluster.id)
  const operations = useClusterOpenOperations(cluster.id)
  const events = useClusterEvents(cluster.id)

  const actionables = deriveClusterActionables({
    cluster,
    pools: pools.data,
    nodes: nodes.data,
    devices: devices.data,
    deployments: deployments.data,
    operations: operations.data,
  })

  const failures: SourceFailure[] = (
    [
      ["Pools", pools.error],
      ["Nodes", nodes.error],
      ["Devices", devices.error],
      ["Deployments", deployments.error],
      ["Operations", operations.error],
    ] as const
  )
    .filter(([, error]) => !!error)
    .map(([label, error]) => ({ label, error }))

  return (
    <div className="space-y-4">
      <Actionables
        actionables={actionables}
        isLoading={
          pools.isLoading ||
          nodes.isLoading ||
          devices.isLoading ||
          deployments.isLoading ||
          operations.isLoading
        }
        failures={failures}
      />

      <GlanceTiles
        cluster={cluster}
        pools={pools}
        nodes={{
          summary: nodes.data && summariseNodes(nodes.data),
          isLoading: nodes.isLoading,
          error: nodes.error,
        }}
        devices={{
          summary: devices.data && summariseDevices(devices.data),
          isLoading: devices.isLoading,
          error: devices.error,
        }}
        deployments={deployments}
      />

      <ClusterIdentity cluster={cluster} />

      <ClusterEvents
        clusterId={cluster.id}
        events={events.items}
        isLoading={events.isLoading}
        error={events.error}
      />
    </div>
  )
}
