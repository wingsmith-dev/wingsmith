// Package agent is the hangar agent: the process that runs inside a GPU cluster
// and gives the gateway a way in without anything dialling inwards.
//
// It owns three things and delegates the rest. It dials out to every gateway
// replica, keeps those connections up, and reports how many of them are
// established. The wire itself belongs to libs/packages/tunnel, which serves
// HTTP/2 on a connection this package hands it; the cluster-local hop belongs to
// internal/dynamo, which finds this cluster's OpenAI-compatible Dynamo frontend
// Service and forwards to it.
//
// It is not the operator, and it is deliberately a different binary. A cluster
// runs both: the operator is a leader-elected singleton on the control path, and
// the agent scales with traffic on the data path. Sharing a process would put
// control-plane uptime in front of inference, which is the one thing rule 4
// forbids.
//
// # Why the targets are a list
//
// The gateway has stable per-pod identity — a StatefulSet behind a headless
// Service — and this dials each of those names rather than one load-balanced
// name. Coverage then becomes a fact instead of a hope: a gateway
// replica can only serve a cluster it holds a tunnel to, so a dial that a load
// balancer sent to the wrong replica is not a connection, it is a gap that only
// shows up as ErrNoTunnel under traffic. Each target is kept up independently
// and each target's tunnel count is exported, so partial coverage is visible
// before it is an incident rather than after.
package agent
