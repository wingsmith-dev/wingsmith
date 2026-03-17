import { NotFoundError, page, registerFixtures } from "@/lib/api"
import { clusterSummary, db } from "@/modules/supply/data"
import type { ClusterDetail } from "./types"

function find(clusterId: string): ClusterDetail {
  const cluster = db.clusters.find((c) => c.id === clusterId)
  if (!cluster) throw new NotFoundError(`No cluster ${clusterId}`)
  return cluster
}

registerFixtures([
  {
    method: "GET",
    path: "/clusters",
    handler: ({ query }) => {
      const region = query.region ? String(query.region) : undefined
      const matched = region
        ? db.clusters.filter((c) => c.region === region)
        : db.clusters
      return page(matched.map(clusterSummary))
    },
  },
  {
    method: "GET",
    path: "/clusters/:clusterId",
    handler: ({ params }) => ({ cluster: find(params.clusterId) }),
  },
  {
    method: "GET",
    path: "/clusters/:clusterId/nodes",
    handler: ({ params }) => {
      find(params.clusterId)
      return page(db.nodes.filter((n) => n.cluster_id === params.clusterId))
    },
  },
  {
    method: "GET",
    path: "/clusters/:clusterId/devices",
    handler: ({ params }) => {
      find(params.clusterId)
      return page(db.devices.filter((d) => d.cluster_id === params.clusterId))
    },
  },
  {
    method: "GET",
    path: "/clusters/:clusterId/topology-domains",
    handler: ({ params }) => {
      find(params.clusterId)
      return page(db.domains.filter((t) => t.cluster_id === params.clusterId))
    },
  },
])
