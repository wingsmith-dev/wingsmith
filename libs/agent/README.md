# agent

The cluster end of hangar's data plane. One Deployment per GPU cluster, `hangar-agent`, and it is
how an inference request gets into a cluster at all.

It dials **out**. An operator owns every cluster but not always the networks they sit in, so nothing ever
dials into a GPU cluster — not the gateway, not the plane, not a laptop. The agent opens the
connection and the request travels back down it.

A cluster runs two hangar processes and this is one of them. The operator operates the cluster: it
deploys workloads and reports observed state up the sync stream. The agent carries traffic. They
share neither a process nor a connection, because if a request needed the operator's connection then
control-plane uptime would be the inference SLA.

## The three things it does

1. **Dial and authenticate.** It is given one address and finds every replica behind it. A gateway
   replica can only serve a cluster it holds a tunnel to, so coverage has to be complete — but each
   accepted handshake names the replica that took the dial and says how many there are, so the agent
   redials the one address, keys what it holds by replica, treats a landing on one it already covers
   as ordinary rather than as an error, and stops when it has them all. That is why a cluster is
   never configured with the shape of the control plane: the plane can scale its gateway without an
   edit reaching a single cluster. The credential is the cluster's enrolment token, the same one the
   operator uses; the gateway resolves it to a cluster identity through the plane.
2. **Serve HTTP/2 on that connection, with the roles reversed.** Having dialled out, the agent runs
   the HTTP/2 *server* and the gateway runs the HTTP/2 *client*. The wire is
   [`libs/packages/tunnel`](../packages/tunnel), which implements both ends.
3. **Proxy to Dynamo's frontend Service.** The cluster-local hop, discovered by asking the cluster:
   the Service that selects on Dynamo's frontend component label, resolved to a ClusterIP. From that
   point on the request never leaves the cluster.

## Reading a partial coverage

`/readyz` reports how many replicas have been reached against how many the gateway says there are,
and one tunnel is enough to be ready — an agent that waited for full coverage would fail its own
rollout because one gateway replica happened to be restarting. The shortfall is a metric, not a
readiness failure: `hangar_agent_gateways_expected` against the number of
`hangar_agent_gateway_up` series, with `hangar_agent_dial_duplicates_total` as the one to watch if
coverage never settles.

## Why it is not part of libs/gateway

Separate module, deliberately. It runs in a cluster we operate but do not deploy from here, on that
cluster's own `helm upgrade` cadence, while the gateway is a deployment this repository applies
directly. Different places, different lifecycles — and the compiler enforces the split: this cannot
reach the routing table or the plane client, and the gateway cannot reach the Kubernetes client this
needs to find Dynamo. The tunnel module is the only thing the two share.
