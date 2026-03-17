import { NotFoundError, page, registerFixtures } from "@/lib/api"
import { db } from "./data"
import type { Device, NodeDetail, Pool } from "./types"

function findNode(nodeId: string): NodeDetail {
  const node = db.nodes.find((n) => n.id === nodeId)
  if (!node) throw new NotFoundError(`No node ${nodeId}`)
  return {
    ...node,
    topology_domains: db.domains.filter((t) =>
      node.topology_domain_ids.includes(t.id)
    ),
  }
}

function findDevice(deviceId: string): Device {
  const device = db.devices.find((d) => d.id === deviceId)
  if (!device) throw new NotFoundError(`No device ${deviceId}`)
  return device
}

function findPool(poolId: string): Pool {
  const pool = db.pools.find((p) => p.id === poolId)
  if (!pool) throw new NotFoundError(`No pool ${poolId}`)
  return pool
}

function filterNodes(query: Record<string, unknown>) {
  return db.nodes.filter((node) => {
    if (query.cluster_id && node.cluster_id !== query.cluster_id) return false
    if (query.pool_id && node.pool_id !== query.pool_id) return false
    if (query.condition && node.condition !== query.condition) return false
    if (query.cordoned !== undefined && query.cordoned !== "")
      return node.cordoned === (String(query.cordoned) === "true")
    return true
  })
}

function filterDevices(query: Record<string, unknown>) {
  return db.devices.filter((device) => {
    if (query.cluster_id && device.cluster_id !== query.cluster_id) return false
    if (query.node_id && device.node_id !== query.node_id) return false
    if (
      query.topology_domain_id &&
      device.topology_domain_id !== query.topology_domain_id
    )
      return false
    if (query.device_class && device.device_class !== query.device_class)
      return false
    if (query.health && device.health !== query.health) return false
    if (query.allocated !== undefined && query.allocated !== "")
      return !!device.allocation === (String(query.allocated) === "true")
    return true
  })
}

registerFixtures([
  {
    method: "GET",
    path: "/nodes",
    handler: ({ query }) => page(filterNodes(query)),
  },
  {
    method: "GET",
    path: "/nodes/:nodeId",
    handler: ({ params }) => ({ node: findNode(params.nodeId) }),
  },
  {
    method: "GET",
    path: "/nodes/:nodeId/devices",
    handler: ({ params }) => {
      findNode(params.nodeId)
      return page(db.devices.filter((d) => d.node_id === params.nodeId))
    },
  },
  {
    method: "GET",
    path: "/devices",
    handler: ({ query }) => page(filterDevices(query)),
  },
  {
    method: "GET",
    path: "/devices/:deviceId",
    handler: ({ params }) => ({ device: findDevice(params.deviceId) }),
  },
  {
    method: "GET",
    path: "/topology-domains",
    handler: ({ query }) =>
      page(
        db.domains.filter(
          (t) => !query.cluster_id || t.cluster_id === query.cluster_id
        )
      ),
  },
  {
    method: "GET",
    path: "/pools",
    handler: ({ query }) =>
      page(
        db.pools.filter(
          (p) => !query.cluster_id || p.cluster_id === query.cluster_id
        )
      ),
  },
  {
    method: "GET",
    path: "/pools/:poolId",
    handler: ({ params }) => ({ pool: findPool(params.poolId) }),
  },
  {
    method: "GET",
    path: "/pools/:poolId/nodes",
    handler: ({ params }) => {
      findPool(params.poolId)
      return page(db.nodes.filter((n) => n.pool_id === params.poolId))
    },
  },
])
