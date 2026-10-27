// Package agent is the hangar agent: the process that runs inside a GPU cluster
// and gives the gateway a way in without anything dialling inwards.
//
// It owns three things and delegates the rest. It dials out to the gateway,
// keeps a tunnel up to every gateway replica, and reports how many of them are
// established. The wire itself belongs to libs/packages/tunnel, which serves
// HTTP/2 on a connection this package hands it; the cluster-local hop belongs to
// internal/dynamo, which finds this cluster's OpenAI-compatible Dynamo frontend
// Service and forwards to it.
//
// It is not the operator, and it is intentionally a different binary. A cluster
// runs both: the operator is a leader-elected singleton on the control path, and
// the agent scales with traffic on the data path. Sharing a process would put
// control-plane uptime in front of inference.
//
// # One address, every replica
//
// The agent is configured with one gateway address, normally a load-balanced
// name, and never with a list of replicas. A gateway replica can only serve a
// cluster it holds a tunnel to, so coverage still has to be complete; the
// handshake is what makes that possible without a list. Every accepted dial
// names the replica that took it and how many replicas there are, so the
// Supervisor redials the one address until each replica holds its quota of
// tunnels, and tells the gateway on every dial which replicas are already
// covered so a landing on one of them is answered, not adopted. Scaling
// the gateway is then a change to the gateway alone. Coverage per replica is
// exported, so a replica the load balancer never reaches is visible as a gap in
// hangar_agent_gateway_up, not as ErrNoTunnel under traffic.
package agent
